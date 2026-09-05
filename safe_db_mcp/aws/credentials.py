"""Where the database password comes from, and why it usually should not exist.

Three sources, chosen by ``SAFEDB_CREDENTIALS``:

``env``
    Read the password from an environment variable. Fine for a local Postgres
    in development. Not what you want in front of a real database.

``secretsmanager``
    Fetch an AWS Secrets Manager secret and read ``username``/``password`` out
    of it. This is the shape RDS creates when you let it manage the master
    credentials, so the same code works against a secret RDS rotates for you.
    The password still exists, but it lives in one place, is rotatable, and is
    never in a file, an image or a shell history.

``rds-iam``
    Ask ``boto3`` for an RDS IAM authentication token and use that as the
    password. **There is no stored password at all.** The token is derived from
    the caller's IAM identity, is scoped to one database user on one host, and
    expires after 15 minutes.

That last one is the interesting one for this project, because it is the same
idea as the write gate one layer down. ``confirm_change`` will not take a
password, it takes a short-lived single-use ``change_id`` that something else
had to mint. RDS IAM auth will not take a password either, it takes a
short-lived token that AWS had to mint. In both cases the durable secret is
replaced by a capability that expires, and in both cases that is enforced by the
system rather than promised by a policy document.

``boto3`` is an optional dependency. Import it only when an AWS source is
actually selected, so the SQLite default stays dependency-free.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Literal

#: Environment variable selecting where credentials come from.
CREDENTIALS_ENV = "SAFEDB_CREDENTIALS"

#: The supported credential sources.
CredentialSource = Literal["env", "secretsmanager", "rds-iam"]

VALID_SOURCES: tuple[CredentialSource, ...] = ("env", "secretsmanager", "rds-iam")

#: RDS IAM tokens are valid for 15 minutes. Stated here so callers do not have
#: to guess; nothing in this module caches a token that long.
RDS_IAM_TOKEN_LIFETIME_SECONDS = 900


class CredentialError(RuntimeError):
    """Raised when credentials cannot be resolved, with a reason and a fix."""


@dataclass(frozen=True)
class AwsSettings:
    """How to talk to AWS, if we are talking to AWS at all.

    Attributes:
        source: Which credential source to use.
        region: AWS region. Falls back to the usual boto3 resolution when unset.
        secret_id: Secrets Manager secret name or ARN, for the
            ``secretsmanager`` source.
    """

    source: CredentialSource = "env"
    region: str | None = None
    secret_id: str | None = None

    @classmethod
    def from_env(cls) -> AwsSettings:
        """Build settings from ``SAFEDB_*`` and standard AWS environment vars."""
        source = (os.environ.get(CREDENTIALS_ENV) or "env").strip().lower()
        if source not in VALID_SOURCES:
            raise CredentialError(
                f"{CREDENTIALS_ENV}='{source}' is not recognised. "
                f"Use one of: {', '.join(VALID_SOURCES)}."
            )
        return cls(
            source=source,  # type: ignore[arg-type]
            region=os.environ.get("SAFEDB_AWS_REGION") or os.environ.get("AWS_REGION"),
            secret_id=os.environ.get("SAFEDB_AWS_SECRET_ID"),
        )


@dataclass(frozen=True)
class DatabaseLogin:
    """A resolved username and password, ready to hand to the driver.

    ``password`` may be a short-lived RDS IAM token rather than a real password.
    Nothing downstream needs to know the difference, which is the point.
    """

    username: str
    password: str
    #: True when the password is a token that will expire.
    ephemeral: bool = False

    def __repr__(self) -> str:  # pragma: no cover - defensive, not behaviour
        """Never render the secret, even in a traceback or a log line."""
        return f"DatabaseLogin(username={self.username!r}, password='***')"


def _import_boto3():
    """Import boto3, turning a missing optional dependency into a clear error."""
    try:
        import boto3  # noqa: PLC0415 - deliberately lazy, it is optional
    except ImportError as error:  # pragma: no cover - depends on the install
        raise CredentialError(
            "This credential source needs boto3, which is an optional dependency. "
            "Install it with: pip install 'safe-db-mcp[aws]'"
        ) from error
    return boto3


def resolve_from_env(username: str, password_env: str) -> DatabaseLogin:
    """Read a password out of an environment variable.

    Args:
        username: The database user to connect as.
        password_env: Name of the variable holding that user's password.

    Raises:
        CredentialError: If the variable is unset or empty.
    """
    password = os.environ.get(password_env)
    if not password:
        raise CredentialError(
            f"{password_env} is not set. Either set it, or select another source "
            f"with {CREDENTIALS_ENV}=secretsmanager or {CREDENTIALS_ENV}=rds-iam."
        )
    return DatabaseLogin(username=username, password=password)


def resolve_from_secrets_manager(settings: AwsSettings, client=None) -> DatabaseLogin:
    """Fetch a username and password from AWS Secrets Manager.

    Expects the JSON shape RDS uses for managed master credentials, which has at
    least ``username`` and ``password`` keys. Extra keys such as ``host`` and
    ``dbname`` are ignored here; connection targeting is separate configuration.

    Args:
        settings: Must carry a ``secret_id``.
        client: An injected boto3 client. Tests pass a stubbed one; production
            leaves it as ``None`` so a real client is built.

    Raises:
        CredentialError: If no secret is configured, the secret has no string
            value, is not JSON, or is missing the expected keys.
    """
    if not settings.secret_id:
        raise CredentialError(
            "SAFEDB_AWS_SECRET_ID is not set, but SAFEDB_CREDENTIALS=secretsmanager. "
            "Set it to the secret name or ARN holding the database credentials."
        )

    if client is None:
        client = _import_boto3().client("secretsmanager", region_name=settings.region)

    response = client.get_secret_value(SecretId=settings.secret_id)
    raw = response.get("SecretString")
    if not raw:
        raise CredentialError(
            f"Secret '{settings.secret_id}' has no SecretString. "
            "A binary secret cannot be used as database credentials."
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CredentialError(
            f"Secret '{settings.secret_id}' is not JSON. Expected an object with "
            "'username' and 'password' keys, which is the format RDS creates."
        ) from error

    missing = [key for key in ("username", "password") if not payload.get(key)]
    if missing:
        raise CredentialError(
            f"Secret '{settings.secret_id}' is missing: {', '.join(missing)}. "
            "Expected the RDS credential shape with 'username' and 'password'."
        )

    return DatabaseLogin(username=payload["username"], password=payload["password"])


def resolve_rds_iam_token(
    settings: AwsSettings, host: str, port: int, username: str, client=None
) -> DatabaseLogin:
    """Generate a short-lived RDS IAM authentication token to use as a password.

    No password is stored anywhere. The token is signed from the caller's IAM
    identity and is only valid for that exact host, port and database user, for
    :data:`RDS_IAM_TOKEN_LIFETIME_SECONDS`.

    The database user must have been granted ``rds_iam`` in Postgres, and the
    IAM principal needs ``rds-db:connect`` on the matching
    ``arn:aws:rds-db:<region>:<account>:dbuser:<resource-id>/<user>``.

    Args:
        settings: Supplies the region.
        host: The RDS endpoint hostname.
        port: The database port.
        username: The database user to authenticate as.
        client: An injected boto3 RDS client, for tests.

    Raises:
        CredentialError: If no region can be determined or AWS returns no token.
    """
    if client is None:
        boto3 = _import_boto3()
        client = boto3.client("rds", region_name=settings.region)

    region = settings.region or getattr(getattr(client, "meta", None), "region_name", None)
    if not region:
        raise CredentialError(
            "No AWS region for RDS IAM auth. Set SAFEDB_AWS_REGION or AWS_REGION."
        )

    token = client.generate_db_auth_token(
        DBHostname=host, Port=port, DBUsername=username, Region=region
    )
    if not token:
        raise CredentialError("AWS returned an empty RDS IAM authentication token.")

    return DatabaseLogin(username=username, password=token, ephemeral=True)
