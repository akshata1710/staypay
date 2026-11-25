"""Application settings, loaded from environment variables (prefix STAYPAY_)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STAYPAY_", env_file=".env", extra="ignore")

    env: str = "local"  # local | test | staging | prod
    database_url: str = "postgresql+asyncpg://staypay:staypay@localhost:5432/staypay"
    db_pool_size: int = 20
    db_max_overflow: int = 40
    redis_url: str = "redis://localhost:6379/0"

    # Processor (MockPay)
    mockpay_url: str = "http://localhost:8100"
    mockpay_api_key: str = "sk_test_staypay"
    processor_timeout_s: float = 10.0
    processor_max_attempts: int = 2

    # Webhooks
    webhook_secret: str = "whsec_dev_secret"
    webhook_tolerance_s: int = 300
    webhook_max_defers: int = 20

    # Payments
    payment_deadline_days_before_checkin: int = 3
    max_installments: int = 4
    stuck_processing_after_s: int = 300

    # Idempotency
    idempotency_wait_s: float = 10.0
    idempotency_stale_after_s: int = 60
    idempotency_key_retention_h: int = 24  # completed keys older than this are deleted (SP-104)
    idempotency_cleanup_batch_size: int = 1000

    # Rate limiting (per guest, per minute) on checkout
    checkout_rate_limit_per_min: int = 10

    # History cache
    history_cache_ttl_s: int = 3600
    history_immutable_after_s: int = 60

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
