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
    SENTRY_DSN_APIS: str | None = Field(
        default=None, description="Sentry DSN. Unset turns Sentry off."
    )

    # CD-036. Credentials the request-metrics middleware publishes to
    # CloudWatch with: the fleet's API identity, the same key pair
    # api-kaianolevine-com publishes its metrics with (the EVALUATION_ prefix
    # is historical; that IAM user holds cloudwatch:PutMetricData). Unset
    # falls back to boto3's default chain. Only read in production, where
    # the middleware records anything at all.
    AWS_REGION: str = Field(default="us-east-1", description="AWS region.")
    EVALUATION_QUEUE_PRODUCER_KEY_ID: str | None = Field(
        default=None, description="AWS access key id for PutMetricData."
    )
    EVALUATION_QUEUE_PRODUCER_SECRET: str | None = Field(
        default=None, description="AWS secret access key for PutMetricData."
    )

    # POST /v1/feedback emails through Brevo when a key is set; unset, the
    # feedback is accepted and no email is sent (deejaytools-api feedback.ts).
    # BREVO_API_KEY is the legacy name, read only when the prefixed one is
    # unset or empty: in the shared Doppler config the unprefixed name is
    # api-kaianolevine-com's, a different Brevo account.
    DEEJAYTOOLS_BREVO_API_KEY: str | None = Field(
        default=None, description="Brevo API key for feedback emails."
    )
    BREVO_API_KEY: str | None = Field(
        default=None,
        description="Legacy name for the Brevo key, read when the prefixed is unset.",
    )

    @property
    def clerk_issuer(self) -> str | None:
        """The trusted Clerk issuer (CLERK_ISSUER), read from
        ``DEEJAYTOOLS_CLERK_ISSUER``; the bare name is another tenant's."""
        return self.DEEJAYTOOLS_CLERK_ISSUER or None

    @property
    def clerk_jwks_url(self) -> str | None:
        """That issuer's JWKS (CLERK_JWKS_URL), read from
        ``DEEJAYTOOLS_CLERK_JWKS_URL``."""
        return self.DEEJAYTOOLS_CLERK_JWKS_URL or None

    @property
    def brevo_api_key(self) -> str | None:
        """The Brevo key to use, as ``DEEJAYTOOLS_BREVO_API_KEY || BREVO_API_KEY``."""
        return self.DEEJAYTOOLS_BREVO_API_KEY or self.BREVO_API_KEY or None

    # Discord notifications (services/notifications.py): the fleet's shared
    # channels, so unprefixed, the names mini_app_polis.discord reads. Each
    # channel reads its own variable and falls back to DISCORD_WEBHOOK_URL;
    # with neither set, that channel's notifications are off (logged once).
    # Declared here so the webhooks resolve from the same place as
    # everything else. Only errors and
    # activity are posted to.
    DISCORD_WEBHOOK_URL: str | None = Field(
        default=None, description="Fallback webhook for every channel."
    )
    DISCORD_WEBHOOK_URL_ERRORS: str | None = Field(
        default=None, description="Webhook for 5xx, unhandled and background faults."
    )
    DISCORD_WEBHOOK_URL_ACTIVITY: str | None = Field(
        default=None, description="Webhook for data changes and songs added."
    )
    # The change feed's switch, as api-kaianolevine-com has it: off mutes
    # the per-request "data changed" summaries without a deploy, and leaves
    # faults, and the "song added" announcements, as they are. Unsetting a
    # webhook cannot do that: activity would fall back to
    # DISCORD_WEBHOOK_URL, and unsetting that silences errors too.
    NOTIFY_DATA_CHANGES: bool = Field(
        default=True, description="Post per-request data-change summaries."
    )

    # Google Drive (deejaytools-api DRIVE.md, "Configuration"). Unprefixed:
    # the names deejaytools-api reads. All three are required by every Drive
    # call; an empty value counts as missing, as it does there.
    GOOGLE_SERVICE_ACCOUNT_EMAIL: str | None = Field(
        default=None, description="Service account the Drive layer acts as."
    )
    GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY: str | None = Field(
        default=None,
        description="Its PEM private key; literal \\n sequences become newlines.",
    )
    GOOGLE_DRIVE_PARENT_FOLDER_ID: str | None = Field(
        default=None, description="Root Drive folder every song file lives under."
    )

    # Scheduler and its operator route (deejaytools-api DRIVE.md "Processing",
    # ADR-006 point 5, ADR-007). Unset TICK_SECRET makes GET /internal/tick
    # refuse every call; an empty string is a set secret.
    TICK_SECRET: str | None = Field(
        default=None, description="Shared secret for GET /internal/tick."
    )
    TICK_INTERVAL_MS: int = Field(
        default=30000, description="Milliseconds between scheduler passes."
    )
    DISABLE_SCHEDULER: bool = Field(
        default=False,
        description="'1' turns the in-process scheduler off (as deejaytools-api).",
    )

    @field_validator("DISABLE_SCHEDULER", mode="before")
    @classmethod
    def scheduler_flag(cls, v: object) -> object:
        """Read the flag as deejaytools-api does: only the string '1' disables.

        Any other string (including 'true' or '0') leaves the scheduler on,
        rather than failing settings validation at boot.
        """
        if isinstance(v, str):
            return v == "1"
        return v

    @field_validator("DEEJAYTOOLS_CORS_ORIGINS", mode="before")
    @classmethod
    def split_origins(cls, v: object) -> object:
        """Split a comma-separated origin list, dropping blanks."""
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    # The process environment only: Doppler supplies it (`doppler run`
    # locally, the Railway sync deployed). No .env file is read.
    model_config = SettingsConfigDict(extra="ignore")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings instance for this process."""
    return Settings()
