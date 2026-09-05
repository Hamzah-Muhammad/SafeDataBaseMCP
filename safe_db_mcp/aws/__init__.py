"""AWS integration: where database credentials come from in a real deployment.

Optional. Nothing in here is imported unless ``SAFEDB_CREDENTIALS`` selects an
AWS source, so the SQLite default never needs boto3 installed.
"""

from .credentials import (
    AwsSettings,
    CredentialError,
    DatabaseLogin,
    resolve_from_env,
    resolve_from_secrets_manager,
    resolve_rds_iam_token,
)

__all__ = [
    "AwsSettings",
    "CredentialError",
    "DatabaseLogin",
    "resolve_from_env",
    "resolve_from_secrets_manager",
    "resolve_rds_iam_token",
]
