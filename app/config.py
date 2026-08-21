import os
from typing import Any

import boto3
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_publishable_key: str = ""
    events_table: str
    orders_table: str
    tickets_table: str
    discount_codes_table: str
    waitlist_table: str
    ses_sender_email: str
    cognito_user_pool_id: str
    cognito_app_client_id: str
    cognito_domain: str = ""
    session_cookie_name: str = "jr_session"
    session_secret: str = "dev-only-insecure-secret"
    aws_region: str = "us-east-1"
    base_url: str = "http://localhost:8000"


SECURE_FIELDS = (
    "stripe_secret_key",
    "stripe_webhook_secret",
    "stripe_publishable_key",
    "session_secret",
)


def _load_secure_params() -> dict[str, str]:
    """Fetch SecureString parameters in one call, or nothing when unset.

    `SECURE_PARAM_PREFIX` is set only by the CDK stack, so locally and under
    pytest this short-circuits and the plain environment variables win.
    """
    prefix = os.environ.get("SECURE_PARAM_PREFIX")
    if not prefix:
        return {}
    client = boto3.client("ssm")
    resp = client.get_parameters(
        Names=[f"{prefix}/{name}" for name in SECURE_FIELDS], WithDecryption=True
    )
    return {p["Name"].rsplit("/", 1)[-1]: p["Value"] for p in resp["Parameters"]}


def _apply_secure_params(target: Settings) -> None:
    for key, value in _load_secure_params().items():
        if value and hasattr(target, key):
            setattr(target, key, value)


settings = Settings()
_apply_secure_params(settings)
