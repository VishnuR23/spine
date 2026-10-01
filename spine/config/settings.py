import logging

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

_WEAK_SECRETS = frozenset(
    {
        "",
        "change-me",
        "dev-change-me-in-production",
        "change-me-long-random-string",
        "REQUIRED_long_random_password",
        "REQUIRED_long_random_api_key",
        "REQUIRED_long_random_jwt_secret",
        "REQUIRED_long_random_string",
    }
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Spine"
    environment: str = "local"
    log_level: str = "INFO"

    database_url: str = "sqlite+aiosqlite:///./spine.db"
    admin_api_key: str = "change-me"
    jwt_secret: str = Field(default="", validation_alias="JWT_SECRET")

    @property
    def effective_jwt_secret(self) -> str:
        return self.jwt_secret or self.admin_api_key

    # OpenTelemetry (optional)
    otel_enabled: bool = Field(default=False, validation_alias="OTEL_ENABLED")
    otel_service_name: str = Field(default="spine-api", validation_alias="OTEL_SERVICE_NAME")
    # Example: http://127.0.0.1:4318/v1/traces
    otel_exporter_otlp_endpoint: str = Field(
        default="http://127.0.0.1:4318/v1/traces", validation_alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )

    # Webhooks (optional SIEM export)
    webhook_timeout_seconds: float = Field(default=2.0, validation_alias="WEBHOOK_TIMEOUT_SECONDS")

    # Optional OIDC (enables admin JWT in addition to ADMIN_API_KEY)
    oidc_jwks_url: str | None = Field(default=None, validation_alias="OIDC_JWKS_URL")
    oidc_audience: str | None = Field(default=None, validation_alias="OIDC_AUDIENCE")
    oidc_issuer: str | None = Field(default=None, validation_alias="OIDC_ISSUER")
    oidc_jwt_algorithms: str = Field(default="RS256", validation_alias="OIDC_JWT_ALGORITHMS")
    oidc_admin_role: str = Field(default="spine_admin", validation_alias="OIDC_ADMIN_ROLE")

    # Plan-bound reviewer LLM (optional; requires Redis + Celery worker)
    anthropic_api_key: str = Field(default="", validation_alias="ANTHROPIC_API_KEY")
    monitor_model: str = Field(default="claude-sonnet-5-5", validation_alias="MONITOR_MODEL")
    redis_url: str = Field(default="redis://redis:6379/0", validation_alias="REDIS_URL")
    celery_broker_url: str = Field(default="", validation_alias="CELERY_BROKER_URL")
    # Hot-path Redis calls (policy cache read, event publish, task enqueue)
    # give up after this long, then skip Redis for redis_retry_after_seconds.
    # See spine/core/redis_guard.py.
    redis_hot_path_timeout_ms: int = Field(default=50, validation_alias="REDIS_HOT_PATH_TIMEOUT_MS")
    redis_retry_after_seconds: float = Field(default=5.0, validation_alias="REDIS_RETRY_AFTER_SECONDS")
    # When an audit event was policy-blocked, skip the reviewer (it never ran).
    monitor_skip_if_policy_blocked: bool = Field(default=True, validation_alias="MONITOR_SKIP_IF_POLICY_BLOCKED")
    sse_enabled: bool = Field(default=True, validation_alias="SSE_ENABLED")
    sse_heartbeat_seconds: int = Field(default=25, validation_alias="SSE_HEARTBEAT_SECONDS")
    approval_grant_ttl_seconds: int = Field(default=3600, validation_alias="APPROVAL_GRANT_TTL_SECONDS")
    celery_task_always_eager: bool = Field(default=False, validation_alias="CELERY_TASK_ALWAYS_EAGER")

    # Plan-bound monitoring
    plan_drift_flag_threshold: float = Field(default=0.4, validation_alias="PLAN_DRIFT_FLAG_THRESHOLD")
    plan_drift_block_threshold: float = Field(default=0.6, validation_alias="PLAN_DRIFT_BLOCK_THRESHOLD")
    plan_eval_history_window: int = Field(default=10, validation_alias="PLAN_EVAL_HISTORY_WINDOW")

    # Security
    login_rate_limit_per_minute: int = Field(default=20, validation_alias="LOGIN_RATE_LIMIT_PER_MINUTE")
    metadata_max_bytes: int = Field(default=8192, validation_alias="METADATA_MAX_BYTES")
    reasoning_api_max_chars: int = Field(default=500, validation_alias="REASONING_API_MAX_CHARS")
    metrics_require_admin_key: bool = Field(default=True, validation_alias="METRICS_REQUIRE_ADMIN_KEY")
    block_webhook_private_urls: bool = Field(default=True, validation_alias="BLOCK_WEBHOOK_PRIVATE_URLS")

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in {"production", "prod"}

    @property
    def effective_celery_broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    @model_validator(mode="after")
    def _warn_weak_secrets_in_production(self) -> "Settings":
        if not self.is_production:
            return self
        problems: list[str] = []
        if self.admin_api_key in _WEAK_SECRETS:
            problems.append("ADMIN_API_KEY must be set to a strong random value in production")
        if not self.jwt_secret or self.jwt_secret in _WEAK_SECRETS:
            problems.append("JWT_SECRET must be set separately from ADMIN_API_KEY in production")
        elif self.jwt_secret == self.admin_api_key:
            problems.append("JWT_SECRET should differ from ADMIN_API_KEY in production")
        elif len(self.jwt_secret.encode()) < 32:
            # HS256 keys shorter than the 256-bit hash output are brute-forceable (RFC 7518 §3.2).
            problems.append("JWT_SECRET must be at least 32 bytes in production")
        if problems:
            joined = "; ".join(problems)
            raise ValueError(joined)
        return self


settings = Settings()
