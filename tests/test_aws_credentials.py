"""Credential resolution, with AWS stubbed out.

No network, no keys, no account. ``botocore``'s ``Stubber`` asserts the exact
API call that would have been made and returns a canned response, so these are
as deterministic as the rest of the suite while still exercising the real boto3
client objects rather than a hand-written fake.

What is proved here: the right AWS call is made with the right parameters, the
resulting login is used as a password without anything downstream caring where
it came from, and every failure mode produces a message that says what to fix.
"""

from __future__ import annotations

import json

import pytest

from safe_db_mcp.aws.credentials import (
    RDS_IAM_TOKEN_LIFETIME_SECONDS,
    AwsSettings,
    CredentialError,
    DatabaseLogin,
    resolve_from_env,
    resolve_from_secrets_manager,
    resolve_rds_iam_token,
)

boto3 = pytest.importorskip("boto3", reason="boto3 is an optional dependency")
Stubber = pytest.importorskip("botocore.stub").Stubber

REGION = "ca-central-1"
SECRET_ID = "safedb/library/credentials"


@pytest.fixture
def secrets_client():
    """A real Secrets Manager client with its transport stubbed."""
    client = boto3.client(
        "secretsmanager",
        region_name=REGION,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    with Stubber(client) as stubber:
        yield client, stubber


class TestEnvironmentSource:
    """The local development path."""

    def test_reads_the_password(self, monkeypatch) -> None:
        monkeypatch.setenv("SAFEDB_PG_WRITER_PASSWORD", "hunter2")
        login = resolve_from_env("safedb_writer", "SAFEDB_PG_WRITER_PASSWORD")
        assert login == DatabaseLogin(username="safedb_writer", password="hunter2")
        assert login.ephemeral is False

    def test_missing_variable_says_what_to_do(self, monkeypatch) -> None:
        monkeypatch.delenv("SAFEDB_PG_WRITER_PASSWORD", raising=False)
        with pytest.raises(CredentialError, match="SAFEDB_PG_WRITER_PASSWORD is not set"):
            resolve_from_env("safedb_writer", "SAFEDB_PG_WRITER_PASSWORD")

    def test_empty_variable_counts_as_missing(self, monkeypatch) -> None:
        monkeypatch.setenv("SAFEDB_PG_WRITER_PASSWORD", "")
        with pytest.raises(CredentialError, match="is not set"):
            resolve_from_env("safedb_writer", "SAFEDB_PG_WRITER_PASSWORD")


class TestSecretsManagerSource:
    """The managed-secret path, in the shape RDS creates."""

    def test_reads_username_and_password_from_the_secret(self, secrets_client) -> None:
        client, stubber = secrets_client
        stubber.add_response(
            "get_secret_value",
            {
                "ARN": f"arn:aws:secretsmanager:{REGION}:123456789012:secret:{SECRET_ID}",
                "Name": SECRET_ID,
                "SecretString": json.dumps(
                    {
                        "username": "safedb_writer",
                        "password": "rotated-by-rds",
                        "engine": "postgres",
                        "host": "safedb.abc123.ca-central-1.rds.amazonaws.com",
                        "port": 5432,
                        "dbname": "safedb",
                    }
                ),
            },
            {"SecretId": SECRET_ID},
        )

        settings = AwsSettings(source="secretsmanager", region=REGION, secret_id=SECRET_ID)
        login = resolve_from_secrets_manager(settings, client=client)

        assert login.username == "safedb_writer"
        assert login.password == "rotated-by-rds"
        stubber.assert_no_pending_responses()

    def test_a_missing_secret_id_is_refused_before_any_call(self) -> None:
        settings = AwsSettings(source="secretsmanager", region=REGION, secret_id=None)
        with pytest.raises(CredentialError, match="SAFEDB_AWS_SECRET_ID is not set"):
            resolve_from_secrets_manager(settings)

    def test_a_binary_secret_is_refused(self, secrets_client) -> None:
        client, stubber = secrets_client
        stubber.add_response(
            "get_secret_value",
            {"Name": SECRET_ID, "SecretBinary": b"\x00\x01"},
            {"SecretId": SECRET_ID},
        )
        settings = AwsSettings(source="secretsmanager", region=REGION, secret_id=SECRET_ID)
        with pytest.raises(CredentialError, match="has no SecretString"):
            resolve_from_secrets_manager(settings, client=client)

    def test_a_non_json_secret_is_refused(self, secrets_client) -> None:
        client, stubber = secrets_client
        stubber.add_response(
            "get_secret_value",
            {"Name": SECRET_ID, "SecretString": "just-a-password"},
            {"SecretId": SECRET_ID},
        )
        settings = AwsSettings(source="secretsmanager", region=REGION, secret_id=SECRET_ID)
        with pytest.raises(CredentialError, match="is not JSON"):
            resolve_from_secrets_manager(settings, client=client)

    @pytest.mark.parametrize(
        "payload,missing",
        [
            ({"password": "p"}, "username"),
            ({"username": "u"}, "password"),
            ({"username": "u", "password": ""}, "password"),
        ],
    )
    def test_an_incomplete_secret_names_what_is_missing(
        self, secrets_client, payload: dict, missing: str
    ) -> None:
        client, stubber = secrets_client
        stubber.add_response(
            "get_secret_value",
            {"Name": SECRET_ID, "SecretString": json.dumps(payload)},
            {"SecretId": SECRET_ID},
        )
        settings = AwsSettings(source="secretsmanager", region=REGION, secret_id=SECRET_ID)
        with pytest.raises(CredentialError, match=f"is missing: {missing}"):
            resolve_from_secrets_manager(settings, client=client)


class TestRdsIamSource:
    """The path where no password exists at all."""

    def test_generates_a_token_for_the_exact_host_port_and_user(self) -> None:
        # generate_db_auth_token is signed locally rather than being an API
        # call, so there is no HTTP request for Stubber to intercept. A recording
        # client captures the arguments instead.
        calls = []

        class RecordingRds:
            meta = type("meta", (), {"region_name": REGION})()

            def generate_db_auth_token(self, **kwargs):
                calls.append(kwargs)
                return "generated.token.value"

        settings = AwsSettings(source="rds-iam", region=REGION)
        login = resolve_rds_iam_token(
            settings,
            host="safedb.abc123.ca-central-1.rds.amazonaws.com",
            port=5432,
            username="safedb_reader",
            client=RecordingRds(),
        )

        assert calls == [
            {
                "DBHostname": "safedb.abc123.ca-central-1.rds.amazonaws.com",
                "Port": 5432,
                "DBUsername": "safedb_reader",
                "Region": REGION,
            }
        ]
        assert login.username == "safedb_reader"
        assert login.password == "generated.token.value"
        assert login.ephemeral is True

    def test_a_real_boto3_client_exposes_the_method_we_call(self) -> None:
        # Guards against the boto3 API drifting out from under us without any
        # network call: the method must exist and be callable on a real client.
        client = boto3.client(
            "rds",
            region_name=REGION,
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
        assert callable(client.generate_db_auth_token)
        token = client.generate_db_auth_token(
            DBHostname="safedb.abc123.ca-central-1.rds.amazonaws.com",
            Port=5432,
            DBUsername="safedb_reader",
            Region=REGION,
        )
        # A presigned URL, not a stored password. Signed locally, no request.
        assert "X-Amz-Signature=" in token
        assert f"X-Amz-Expires={RDS_IAM_TOKEN_LIFETIME_SECONDS}" in token

    def test_no_region_is_refused_with_a_fix(self) -> None:
        class NoRegion:
            meta = type("meta", (), {"region_name": None})()

            def generate_db_auth_token(self, **kwargs):  # pragma: no cover
                raise AssertionError("should not be reached")

        with pytest.raises(CredentialError, match="Set SAFEDB_AWS_REGION"):
            resolve_rds_iam_token(
                AwsSettings(source="rds-iam", region=None),
                host="h",
                port=5432,
                username="u",
                client=NoRegion(),
            )

    def test_an_empty_token_is_refused(self) -> None:
        class EmptyToken:
            meta = type("meta", (), {"region_name": REGION})()

            def generate_db_auth_token(self, **kwargs):
                return ""

        with pytest.raises(CredentialError, match="empty RDS IAM"):
            resolve_rds_iam_token(
                AwsSettings(source="rds-iam", region=REGION),
                host="h",
                port=5432,
                username="u",
                client=EmptyToken(),
            )


class TestSettingsFromEnvironment:
    """Configuration errors surface at startup, not at first connection."""

    def test_defaults_to_the_environment_source(self, monkeypatch) -> None:
        monkeypatch.delenv("SAFEDB_CREDENTIALS", raising=False)
        assert AwsSettings.from_env().source == "env"

    @pytest.mark.parametrize("source", ["env", "secretsmanager", "rds-iam"])
    def test_accepts_each_supported_source(self, monkeypatch, source: str) -> None:
        monkeypatch.setenv("SAFEDB_CREDENTIALS", source)
        assert AwsSettings.from_env().source == source

    def test_an_unknown_source_lists_the_valid_ones(self, monkeypatch) -> None:
        monkeypatch.setenv("SAFEDB_CREDENTIALS", "vault")
        with pytest.raises(CredentialError, match="env, secretsmanager, rds-iam"):
            AwsSettings.from_env()

    def test_region_falls_back_to_the_standard_aws_variable(self, monkeypatch) -> None:
        monkeypatch.delenv("SAFEDB_AWS_REGION", raising=False)
        monkeypatch.setenv("AWS_REGION", "eu-west-2")
        assert AwsSettings.from_env().region == "eu-west-2"


class TestSecretsAreNotLeaked:
    """A password must not turn up in a log line or a traceback."""

    def test_repr_hides_the_password(self) -> None:
        login = DatabaseLogin(username="safedb_writer", password="super-secret-value")
        assert "super-secret-value" not in repr(login)
        assert "***" in repr(login)
        assert "safedb_writer" in repr(login)

    def test_the_backend_description_carries_no_credentials(self, monkeypatch) -> None:
        psycopg = pytest.importorskip("psycopg")
        assert psycopg is not None
        from safe_db_mcp.backends.postgres_backend import PostgresBackend, PostgresSettings

        monkeypatch.setenv("SAFEDB_PG_WRITER_PASSWORD", "super-secret-value")
        backend = PostgresBackend(PostgresSettings(host="db.example", port=5432, database="safedb"))
        assert "super-secret-value" not in backend.description
        assert backend.description == "postgresql://db.example:5432/safedb"

    def test_the_conninfo_string_is_the_only_place_the_secret_appears(self) -> None:
        from safe_db_mcp.backends.postgres_backend import PostgresSettings

        settings = PostgresSettings(host="db.example", sslmode="require")
        login = DatabaseLogin(username="u", password="super-secret-value")
        conninfo = settings.conninfo(login)
        # It has to be in the connection string; that is what it is for. What
        # matters is that nothing renders the connection string by default.
        assert "super-secret-value" in conninfo
        assert "sslmode='require'" in conninfo
        assert "application_name='safe-db-mcp'" in conninfo
