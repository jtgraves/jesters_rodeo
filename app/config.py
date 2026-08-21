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


settings = Settings()
