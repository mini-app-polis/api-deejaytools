from __future__ import annotations

import json
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
    deejaytools-api read, so its config carried over unchanged.
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

    # The one trusted Clerk issuer (ADR-0008). Unset means
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
    # One Brevo account serves the fleet, so this is the same key
    # api-kaianolevine-com reads.
    BREVO_API_KEY: str | None = Field(
        default=None, description="Brevo API key for feedback emails."
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
        """The Brevo key, or None when ``BREVO_API_KEY`` is unset or empty."""
        return self.BREVO_API_KEY or None

    @property
    def google_service_account(self) -> tuple[str, str] | None:
        """``(client_email, private_key)`` from ``GOOGLE_CREDENTIALS_JSON``.

        None when it is unset, is not a JSON object, or lacks either field.
        Parsed with ``strict=False``: Doppler stores the key's line breaks as
        real newlines, which strict JSON rejects inside a string.
        """
        raw = (self.GOOGLE_CREDENTIALS_JSON or "").strip()
        if not raw:
            return None
        try:
            info = json.loads(raw, strict=False)
        except ValueError:
            return None
        if not isinstance(info, dict):
            return None
        email, key = info.get("client_email"), info.get("private_key")
        if (
            not isinstance(email, str)
            or not email
            or not isinstance(key, str)
            or not key
        ):
            return None
        return email, key

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
    # Which messages each environment posts, keyed by ENVIRONMENT, in the
    # same shape and with the same defaults as api-kaianolevine-com's. The
    # event names are services.notifications.NOTIFY_EVENTS. An environment
    # with no entry posts every event; an event left out of its
    # environment's list is not posted. Both gates apply: data changes also
    # need NOTIFY_DATA_CHANGES. Development leaves data changes out: its
    # writes are contract-suite runs and test cleanup, not news.
    DISCORD_NOTIFY_EVENTS: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "production": ["data_changed", "fault"],
            "development": ["fault"],
        },
        description="Per-environment allow-list of Discord notification events.",
    )

    # Google Drive (docs/DRIVE.md, "Configuration"). The fleet's
    # one service account, as the JSON key file Google issues — the same
    # GOOGLE_CREDENTIALS_JSON every cog reads. It and the parent folder are
    # required by every Drive call; an empty value counts as missing.
    GOOGLE_CREDENTIALS_JSON: str | None = Field(
        default=None, description="The service account's JSON key file."
    )
    GOOGLE_DRIVE_PARENT_FOLDER_ID: str | None = Field(
        default=None, description="Root Drive folder every song file lives under."
    )

    # Scheduler and its operator route (docs/DRIVE.md "Processing",
    # ADR-0007 point 5, ADR-0008). Unset TICK_SECRET makes GET /internal/tick
    # refuse every call; an empty string is a set secret.
    TICK_SECRET: str | None = Field(
        default=None, description="Shared secret for GET /internal/tick."
    )
    # The value TICK_SECRET held before a rotation, accepted alongside it
    # until the rotation retires it. Only extends a set TICK_SECRET (it never
    # opens the route alone), and empty counts as unset.
    TICK_SECRET_PREVIOUS: str | None = Field(
        default=None,
        description="Previous tick secret, accepted during a rotation overlap.",
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
    # locally, the Railway sync deployed). No .env file is read. An empty
    # value is treated as unset, so the default above applies: the vault
    # holds every optional name, empty until it is given a value of its own.
    model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings instance for this process."""
    return Settings()
