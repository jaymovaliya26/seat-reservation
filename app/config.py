"""Runtime settings, read from environment variables (and from .env in local development)."""

from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    database_url: str
    jwt_secret: SecretStr
    jwt_ttl_s: int = Field(default=86_400, ge=60)
    admin_api_key: SecretStr

    # Per worker process. Budget: workers x (db_pool_max + 1 for readiness) per instance, and
    # during a deploy the old and new instance overlap, so twice that must stay below Postgres
    # max_connections (100): 2 x 4 x (8 + 1) = 72. Eight busy connections per worker is plenty
    # for a 2-vCPU Postgres; more would only queue inside the database instead of in the pool.
    # min == max opens every connection at startup, so none is ever created mid-burst, where a
    # refusal would turn into an error; at startup a refusal is simply retried.
    db_pool_min: int = Field(default=8, ge=1)
    db_pool_max: int = Field(default=8, ge=1)

    # Applied to every pooled connection, so no query or lock wait can hang a request forever.
    db_statement_timeout_ms: int = Field(default=5000, ge=0)
    db_lock_timeout_ms: int = Field(default=5000, ge=0)
    db_idle_in_transaction_timeout_ms: int = Field(default=10000, ge=0)

    # How long startup keeps retrying while Postgres is unreachable (cold starts, redeploys).
    db_startup_timeout_s: float = Field(default=60.0, gt=0)
    readiness_timeout_s: float = Field(default=1.0, gt=0)

    default_per_user_limit: int = Field(default=4, ge=1)
    log_level: str = "INFO"
    # Request log lines per second per worker; 0 means no cap. Railway silently drops lines
    # above 500/s per replica and, measured, above roughly 250/s per deployment, so
    # 4 workers x 25 = 100/s stays clear of both.
    log_request_lines_per_second: int = Field(default=25, ge=0)

    @model_validator(mode="after")
    def _pool_bounds(self) -> Self:
        if self.db_pool_min > self.db_pool_max:
            raise ValueError("db_pool_min must not exceed db_pool_max")
        return self
