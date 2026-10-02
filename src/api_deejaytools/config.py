from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from mini_app_polis.environment import current_environment
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables.

    Most names carry a DEEJAYTOOLS_ prefix because they live in the shared
    ecosystem Doppler config, which also holds the other Clerk tenant's
    CLERK_* values and other services' DATABASE_URL. They are the names
    deejaytools-api reads, so one config feeds both services through the
    cutover.
    """

    DEEJAYTOOLS_DATABASE_URL: str = Field(
        description="Postgres connection string for the runtime role."
    )

    # OPS-002. The connection the migration step uses, bound to a role that
    # owns the schema. Declared here so both roles are visible in one place,
    # but never read by anything under src/: scripts/apply_migrations.py
    # reads the environment variable directly, before uvicorn starts.
    DEEJAYTOOLS_DATABASE_URL_MIGRATIONS: str | None = Field(
        default=None,
        description="Postgres connection string for the schema-owning role.",
    )

    # The one trusted Clerk issuer (deejaytools-api ADR-007). Unset means
    # every authenticated request is rejected, as it is in deejaytools-api.
    DEEJAYTOOLS_CLERK_ISSUER: str | None = Field(
        default=None, description="The iss every session JWT must carry."
    )
    DEEJAYTOOLS_CLERK_JWKS_URL: str | None = Field(
        default=None, description="JWKS URL of that Clerk instance."
    )

    # Comma-separated, as deejaytools-api reads the same value.
    DEEJAYTOOLS_CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(
        default=["http://localhost:5173"],
        description="Origins allowed by CORS.",
    )

    ENVIRONMENT: str = Field(
        default_factory=lambda: current_environment().value,
        description="Deployment environment, from the shared fleet resolver.",
    )
    SENTRY_DSN_API_DEEJAYTOOLS: str | None = Field(
        default=None, description="Sentry DSN. Unset turns Sentry off."
    )

    # CD-036. Credentials the request-metrics middleware publishes to
    # CloudWatch with. Unset falls back to boto3's default chain. Only read
    # in production, where the middleware records anything at all.
    AWS_REGION: str = Field(default="us-east-1", description="AWS region.")
    DEEJAYTOOLS_METRICS_KEY_ID: str | None = Field(
        default=None, description="AWS access key id for PutMetricData."
    )
    DEEJAYTOOLS_METRICS_SECRET: str | None = Field(
        default=None, description="AWS secret access key for PutMetricData."
    )

    @field_validator("DEEJAYTOOLS_CORS_ORIGINS", mode="before")
    @classmethod
    def split_origins(cls, v: object) -> object:
        """Split a comma-separated origin list, dropping blanks."""
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings instance for this process."""
    return Settings()
