import logging
import os
import time

import boto3
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# The one secret whose "unset" default is a real, working, publicly-known
# value -- unlike the Stripe fields (default ""), which just fail loudly at
# the first Stripe call. Session cookies signed with this string are
# forgeable by anyone who's read this file. See _apply_secure_params: in the
# deployed environment (SECURE_PARAM_PREFIX set), still landing on this
# value after a load is treated as fatal, not a silent fallback.
_INSECURE_DEFAULT_SESSION_SECRET = "dev-only-insecure-secret"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Resolved from whichever of the stripe_test_*/stripe_live_* SSM pairs
    # stripe_mode currently points at -- see _apply_secure_params. Every
    # route reads these three exactly as before; only this module knows
    # there are two pairs behind them.
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_publishable_key: str = ""
    stripe_mode: str = "test"
    events_table: str
    orders_table: str
    tickets_table: str
    discount_codes_table: str
    waitlist_table: str
    announcements_table: str
    past_beneficiaries_table: str
    faq_entries_table: str
    clown_profiles_table: str
    krewe_links_table: str
    event_images_bucket: str
    ses_sender_email: str
    cognito_user_pool_id: str
    cognito_app_client_id: str
    cognito_domain: str = ""
    # Only read when an admin actually sends an announcement, so local dev
    # and tests that never exercise that path don't need it set.
    announcement_lambda_name: str = ""
    session_cookie_name: str = "jr_session"
    session_secret: str = _INSECURE_DEFAULT_SESSION_SECRET
    aws_region: str = "us-east-1"
    base_url: str = "http://localhost:8000"


# The Stripe test/live pair plus the mode flag that picks between them, and
# session_secret. Fetched together in one SSM call; WithDecryption=True is a
# harmless no-op on stripe_mode (a plain String, not a SecureString).
SECURE_FIELDS = (
    "stripe_mode",
    "stripe_test_secret_key",
    "stripe_test_webhook_secret",
    "stripe_test_publishable_key",
    "stripe_live_secret_key",
    "stripe_live_webhook_secret",
    "stripe_live_publishable_key",
    "session_secret",
)

# How stale the cached Stripe mode/keys are allowed to get before a request
# re-checks SSM. Lambda reuses warm execution environments across requests,
# so without this a stripe_mode flip in SSM would only reach a given
# container at its next cold start -- which could be minutes or hours away.
# See README.md "Switching Stripe modes".
SECURE_PARAMS_TTL_SECONDS = 60

_secure_params_loaded_at = 0.0


def _load_secure_params() -> dict[str, str]:
    """Fetch SecureString/String parameters in one call, or nothing when unset.

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
    raw = _load_secure_params()

    if raw.get("session_secret"):
        target.session_secret = raw["session_secret"]
    elif (
        os.environ.get("SECURE_PARAM_PREFIX")
        and target.session_secret == _INSECURE_DEFAULT_SESSION_SECRET
    ):
        # We're running in the deployed environment (SECURE_PARAM_PREFIX is
        # set only by the CDK stack -- local dev and pytest never reach this
        # branch) and no real session_secret has ever been loaded. Every
        # session cookie would be signed with a value anyone can read in this
        # file. Refuse to start rather than silently serve forgeable
        # sessions; fix by setting "<SecureParamPrefix>/session_secret" in
        # SSM (see README.md).
        raise RuntimeError(
            "session_secret is not set in SSM. Refusing to start with the "
            "public default -- set the session_secret SSM parameter and retry."
        )

    # stripe_mode must never silently fail open into live: anything other
    # than exactly "live" (missing, misspelled, empty) is treated as "test".
    mode = "live" if raw.get("stripe_mode") == "live" else "test"
    target.stripe_mode = mode
    for field in ("secret_key", "webhook_secret", "publishable_key"):
        value = raw.get(f"stripe_{mode}_{field}")
        if value:
            setattr(target, f"stripe_{field}", value)


def refresh_secure_params_if_stale() -> None:
    """Re-check SSM for the Stripe mode/keys if the last fetch is older than
    SECURE_PARAMS_TTL_SECONDS; a cheap no-op otherwise.

    Call this at the few spots that actually use `settings.stripe_*` for a
    live Stripe call or signature check: the checkout route, the Stripe
    webhook handler, and auth._authenticated_claims (which gates every admin
    page view, and so also covers the refund route and the admin nav's mode
    badge). Everywhere else keeps reading `settings.stripe_mode` etc. as a
    plain attribute -- this just keeps it from going stale for too long.
    """
    global _secure_params_loaded_at
    if time.time() - _secure_params_loaded_at < SECURE_PARAMS_TTL_SECONDS:
        return
    _apply_secure_params(settings)
    _secure_params_loaded_at = time.time()


settings = Settings()
_apply_secure_params(settings)
_secure_params_loaded_at = time.time()
