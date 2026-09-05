import logging
import os

import boto3
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


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
    announcements_table: str
    ses_sender_email: str
    cognito_user_pool_id: str
    cognito_app_client_id: str
    cognito_domain: str = ""
    # Only read when an admin actually sends an announcement, so local dev
    # and tests that never exercise that path don't need it set.
    announcement_lambda_name: str = ""
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

    # SSM reports a missing/misnamed parameter by listing it in
    # InvalidParameters rather than by failing. Silently skipping those leaves
    # the app running on an empty default — a webhook secret that validates
    # nothing, a session secret that is the public dev placeholder — with no
    # signal anywhere. One log line per missing name makes it findable in
    # CloudWatch Logs at cold start.
    for name in resp.get("InvalidParameters", []):
        logger.warning(
            "SSM parameter %s is missing or unreadable; %s falls back to its "
            "environment/default value",
            name, name.rsplit("/", 1)[-1],
        )

    return {p["Name"].rsplit("/", 1)[-1]: p["Value"] for p in resp["Parameters"]}


def _apply_secure_params(target: Settings) -> None:
    for key, value in _load_secure_params().items():
        if value and hasattr(target, key):
            setattr(target, key, value)


settings = Settings()
_apply_secure_params(settings)
