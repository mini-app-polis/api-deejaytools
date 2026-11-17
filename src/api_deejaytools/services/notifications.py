"""Discord notifications: the change feed, faults, and feedback.

Faults go through ``mini_app_polis.activity`` and ``mini_app_polis.discord``,
shared with the fleet's other APIs: a middleware that posts every 5xx or
unhandled exception to ``errors``, and background fault reports. The change
feed is this service's own: each change a request commits is kept in
``activity_feed`` in the same transaction (``services/change_feed.py``),
and the scheduler posts one message per person, in words, once they have
been quiet for a minute (``services/activity_feed.py``), to the
``activity`` channel (this service's own when
``DEEJAYTOOLS_DISCORD_WEBHOOK_URL_ACTIVITY`` is set, else the fleet's shared
one). Kept in the database rather than in a worker's memory, because the
API runs several workers and one person's burst used to arrive as one
message or several depending on where their requests landed.

What stays here is this service's policy:

- **Who did it.** The actor is ``request.state.caller``, which
  ``auth.require_scope`` stamps with the caller's Clerk user id (the
  principal's subject); a message is titled with the person's first and
  last name from ``users`` (:func:`user_display_name`), or the id when they
  have none — never their email, which does not belong in a shared
  channel. The name is read when a message is sent, never on a request's
  path.
- **Only production posts.** Changes and faults are posted from production
  only; any other environment says what it changed and what broke in its
  own logs. A message's footer is ``api-deejaytools``, with the environment
  only outside production (:func:`footer`).
- **What is not news** (``SUPPRESSED_TABLES``, ``EXCLUDED_PATHS``,
  ``CHANGES_NOT_REPORTED``), each with its reason. ``NOTIFY_DATA_CHANGES``
  turns the change feed off altogether without a deploy, as in
  api-kaianolevine-com; faults and "song added" are unaffected.
- **Which events each environment posts** (``DISCORD_NOTIFY_EVENTS``,
  :func:`event_enabled`), named and shaped as in api-kaianolevine-com:
  ``data_changed`` is the change feed, ``fault`` is every fault, from a
  request or from background work. Both only ever post from production
  (above), so the list can narrow production's messages, not widen
  development's.
- **No machine callers.** The only caller is the web app, so a 4xx is a
  person meeting a guard and is never reported (``is_machine`` is None).
- **Activity has its own channel.** The change feed and "feedback" post to
  ``DEEJAYTOOLS_DISCORD_WEBHOOK_URL_ACTIVITY`` when it is set, so
  deejaytools' activity is not mixed into the fleet's shared channel;
  unset, they post to ``DISCORD_WEBHOOK_URL_ACTIVITY`` as before.
- **Webhooks resolve from ``Settings``**, like every other setting,
  rather than straight from the environment. With none set,
  notifications are off: the first message dropped on each channel is
  logged, the rest silently, and nothing is posted.
- **Song added** (:func:`record_song_added`): kept in the feed when the
  song's Drive build commits, never for a build that fails or rolls back,
  and posted with the uploader's other changes — joined to the song's
  submission to an event when that came in the same minute.
- **Feedback** (``announce_feedback``): one message per piece of site
  feedback accepted, naming its type and subject, never its sender.
- **Background faults** (``report_fault``, ``report_fault_once``): work with
  no request — the scheduler, song builds, Drive jobs — reports only what
  is final or what stops a whole step, never each retry.

Nothing here raises into a request or a job: a dropped notification is not
a failed request.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from mini_app_polis import activity, discord
from mini_app_polis.logger import LOG_WARNING, get_logger, with_log_prefix
from sqlalchemy import select

from ..config import Settings, get_settings
from ..database import get_sessionmaker
from ..models import User
from . import change_feed

logger = get_logger()

SERVICE = "api-deejaytools"

#: The messages ``DISCORD_NOTIFY_EVENTS`` names: the summary of what a
#: request committed, and a fault (a request's 5xx, or a background fault).
DATA_CHANGED = "data_changed"
FAULT = "fault"
NOTIFY_EVENTS = (DATA_CHANGED, FAULT)


def event_enabled(event: str, settings: Settings) -> bool:
    """Whether ``settings.ENVIRONMENT`` posts ``event`` to Discord.

    An environment with no entry in ``DISCORD_NOTIFY_EVENTS`` posts every
    event. ``NOTIFY_DATA_CHANGES`` is a separate gate.
    """
    lists = {k.lower(): v for k, v in settings.DISCORD_NOTIFY_EVENTS.items()}
    allowed = lists.get(settings.ENVIRONMENT.lower())
    return allowed is None or event in allowed


#: Tables whose writes are bookkeeping rather than news. A request that
#: changed only these posts nothing; one that changed others lists only
#: the others.
SUPPRESSED_TABLES = frozenset(
    {
        # One row per authorization decision, allow and deny alike, on every
        # scoped request — the polled GETs included (the audit sink commits
        # it itself). Leaving it in would make the feed a copy of the access
        # log, and every real change would carry "+1" of it.
        "identity_audit_events",
        # The staged bytes of an upload, written with the song and deleted
        # when its build finishes. The song is what is news, and it is
        # announced on its own when its build succeeds.
        "song_uploads",
        # The Drive job queue: rows enqueued as a side effect of a
        # submission or a delete (already reported under their own table).
        # Failures that are final go to errors. An operator's own changes to
        # the queue are still reported (OPERATOR_QUEUE_PATHS).
        "drive_jobs",
        # Who may do what: written with a change that is news in its own
        # right (a staff grant, an admin role, a sign-in), which says it.
        "identity_issuers",
        "identity_principals",
        "identity_principal_roles",
        "identity_explicit_grants",
        # A dancer and a partner as one check-in entity: made on the way to
        # a check-in or a song, never news by itself.
        "pairs",
    }
)

#: Paths outside the feed entirely, faults included. Liveness and version
#: are polled by uptime monitors and the deploy smoke test, write nothing,
#: and their failures are the monitors' job.
EXCLUDED_PATHS = ("/health", "/version")

#: Paths whose faults are reported but whose changes are not. Each covers
#: the paths below it too (``/v1/checkins`` covers ``/v1/checkins/{id}``).
CHANGES_NOT_REPORTED = frozenset(
    {
        # Every sign-in upserts the users row (email, updated_at) and
        # re-ensures the principal and its dancer role: a change on every
        # login, not news.
        "/v1/auth/sync",
        # The final chunk creates the song (and, for a team or "other"
        # upload, its placeholder partner), but a song is only added once
        # its Drive build succeeds: a failed build deletes it again. The
        # build records it then (record_song_added), naming the
        # placeholder; a tally here would announce songs that never came
        # to exist. Earlier chunks write nothing but the audit row.
        "/v1/songs/upload/chunk",
        # Site feedback saves a row; announce_feedback already posts it,
        # by its type and subject only, so a tally would say it twice.
        "/v1/feedback",
        # The operator's scheduler pass: session status advances, queue
        # fills and Drive jobs, which the background loop runs every 30 s
        # without a tally. The same work is no more news when an operator
        # triggers it. Its failures are reported by the scheduler.
        "/internal/tick",
        # Synthetic check-ins for exercising the floor: a stub leader, its
        # partner, pair and check-in on every injection, and their removal.
        # Test data an admin made on purpose, reported in the shared feed
        # as if dancers had arrived. The admin who made it knows.
        "/v1/admin/checkins",
        # The live floor: every dancer checking in or withdrawing a check-in,
        # and every manager action on the queue (promote, complete,
        # incomplete, move down, withdraw). Dozens a minute at an event,
        # each one routine, which would bury everything else in the shared
        # channel. Faults are still reported.
        "/v1/checkins",
        "/v1/queue",
    }
)

#: Paths where a ``drive_jobs`` write is the point of the request — an
#: admin retrying an exhausted job or queueing renames — so the table is
#: reported there.
OPERATOR_QUEUE_PATHS = ("/v1/admin/drive-jobs",)

#: Discord embed colour for an announcement: the change feed's blue.
_ANNOUNCE_COLOR = 0x58A6FF

#: Channels already logged as having no webhook, so "off" is said once.
_unconfigured_logged: set[str] = set()


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def webhook_source(settings: Settings) -> dict[str, str | None]:
    """The webhook variables as ``Settings`` declares them, by the names the
    library reads.

    Only the channels this service posts to have a variable of their own;
    any other channel falls back to ``DISCORD_WEBHOOK_URL``. Activity goes
    to this service's own channel (``DEEJAYTOOLS_DISCORD_WEBHOOK_URL_ACTIVITY``)
    when it is set, else to the fleet's shared one.
    """
    own_activity = (settings.DEEJAYTOOLS_DISCORD_WEBHOOK_URL_ACTIVITY or "").strip()
    return {
        discord.FALLBACK_ENV: settings.DISCORD_WEBHOOK_URL,
        discord.CHANNEL_ENV[
            discord.CHANNEL_ERRORS
        ]: settings.DISCORD_WEBHOOK_URL_ERRORS,
        discord.CHANNEL_ENV[discord.CHANNEL_ACTIVITY]: (
            own_activity or settings.DISCORD_WEBHOOK_URL_ACTIVITY
        ),
    }


async def send(channel: str, payload: dict[str, Any], context: str) -> bool:
    """Post a built message to ``channel``'s webhook; never raises.

    The library's ``Sender``. With no webhook for the channel (its own
    variable and ``DISCORD_WEBHOOK_URL`` both unset), notifications are off:
    that is logged once per channel and the message is dropped.
    """
    url = discord.webhook_url(channel, source=webhook_source(get_settings()))
    if url is None:
        if channel not in _unconfigured_logged:
            _unconfigured_logged.add(channel)
            logger.warning(
                with_log_prefix(
                    LOG_WARNING,
                    f"discord notifications off: no webhook for channel={channel} "
                    "(DISCORD_WEBHOOK_URL unset)",
                )
            )
        return False
    return await discord.post_webhook(
        url, json=payload, channel=channel, context=context
    )


# ---------------------------------------------------------------------------
# Requests: change feed and faults
# ---------------------------------------------------------------------------


def _config() -> activity.ActivityConfig:
    """The library middleware's config: faults only.

    Changes are this service's own feed (``services/change_feed.py``), so
    the library's per-worker tally is off.
    """
    settings = get_settings()
    return activity.ActivityConfig(
        service=SERVICE,
        display_name=user_display_name,
        environment=settings.ENVIRONMENT,
        suppressed_tables=SUPPRESSED_TABLES,
        excluded_paths=EXCLUDED_PATHS,
        report_changes=False,
        report_faults=event_enabled(FAULT, settings),
        send=send,
        label=True,
    )


async def user_display_name(user_id: str) -> str | None:
    """A person's first and last name from ``users``, for the feed.

    None when they have no row or no name, so the feed shows their id. Only
    the name parts, never ``display_name``: provisioning sets that to the
    email, and the feed's channels are shared. Called by the feed when a
    message is sent, never on the request's path; a failed read is shown as
    the id.
    """
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(User.first_name, User.last_name).where(User.id == user_id)
            )
        ).first()
    if row is None:
        return None
    name = " ".join(
        p.strip() for p in (row.first_name, row.last_name) if p and p.strip()
    )
    return name or None


_faults = activity.activity_middleware(lambda: _config())


def captured_tables_ignored(path: str) -> frozenset[str]:
    """The tables a request's change capture leaves out on ``path``."""
    if activity.is_excluded(path, OPERATOR_QUEUE_PATHS):
        return SUPPRESSED_TABLES - {"drive_jobs"}
    return SUPPRESSED_TABLES


async def activity_middleware(request: Any, call_next: Any) -> Any:
    """Keep what the request changes for the feed, and report any fault.

    Registered outside the error middleware and the request limits, so the
    fault report sees the status that went on the wire (a deadline's 503
    included) and the fault detail ``errors.UnhandledErrorMiddleware``
    records. The capture is open for the request's own work only.
    """
    path = request.url.path
    if activity.is_excluded(path, EXCLUDED_PATHS) or activity.is_excluded(
        path, CHANGES_NOT_REPORTED
    ):
        return await _faults(request, call_next)
    token = change_feed.begin(request, ignored=captured_tables_ignored(path))
    try:
        return await _faults(request, call_next)
    finally:
        change_feed.end(token)


def footer() -> str:
    """A message's footer: the service, and the environment outside production.

    The channel is this service's own and only production posts, so the
    environment would say the same thing on every message.
    """
    environment = get_settings().ENVIRONMENT
    if environment.lower() == "production":
        return SERVICE
    return f"{SERVICE} · {environment}"


# ---------------------------------------------------------------------------
# Background faults
# ---------------------------------------------------------------------------


def report_fault(
    where: str, exc: BaseException, *, event_id: str | None = None
) -> None:
    """Post a fault from work with no request, without waiting on Discord.

    The message carries ``where``, the exception's type and the Sentry id —
    never its text. Pass ``event_id`` when the caller already reported the
    exception to Sentry; nothing is reported to Sentry here, so a fault the
    caller did not report names its type only and the log line has the rest.
    Nothing is posted when the environment leaves ``fault`` out of its list.
    """
    if not event_enabled(FAULT, get_settings()):
        return
    activity.dispatch(
        activity.report_fault(
            where,
            exc,
            event_id=event_id,
            service=SERVICE,
            environment=get_settings().ENVIRONMENT,
            capture=None,
            send=send,
            label=True,
        )
    )


#: Fault keys currently failing, for ``report_fault_once``.
_failing: set[str] = set()


def report_fault_once(
    key: str, where: str, exc: BaseException, *, event_id: str | None = None
) -> None:
    """``report_fault`` for a step that repeats: once per run of failures.

    A scheduler step fails on every tick while its cause lasts (every 30 s),
    so only the first failure is posted; ``clear_fault`` after a success
    re-arms it.
    """
    if key in _failing:
        return
    _failing.add(key)
    report_fault(where, exc, event_id=event_id)


def clear_fault(key: str) -> None:
    """The step behind ``key`` succeeded: its next failure is news again."""
    _failing.discard(key)


def reset() -> None:
    """Forget failing steps and logged channels. For tests."""
    _failing.clear()
    _unconfigured_logged.clear()


# ---------------------------------------------------------------------------
# Song added
# ---------------------------------------------------------------------------

_MARKDOWN = re.compile(r"([\\*_~`|>\[\]])")


def _md(value: str) -> str:
    """Escape Discord markdown in text people typed (names, routine names)."""
    return _MARKDOWN.sub(r"\\\1", value)


def person_name(first: str | None, last: str | None, *fallbacks: str | None) -> str:
    """``First Last``, else the first non-blank fallback, else ``someone``."""
    name = " ".join(p.strip() for p in (first, last) if p and p.strip())
    if name:
        return name
    for fallback in fallbacks:
        if fallback and fallback.strip():
            return fallback.strip()
    return "someone"


def drive_file_url(file_id: str) -> str:
    """The song file's Drive page."""
    return f"https://drive.google.com/file/d/{file_id}/view"


def record_song_added(session: Any, actor_id: str, facts: dict[str, Any]) -> None:
    """Keep "song added" in the feed, in ``session``'s transaction.

    ``actor_id`` is who uploaded it (its owner, or the person who uploaded it
    for them). ``facts`` say what it is: ``song_id``, ``routine``,
    ``division``, ``partner`` and ``partner_kind``, ``managed``, ``for`` (the
    owner's name, when someone else uploaded it) and ``drive_file_id``. A
    build that rolls back keeps nothing. Posted with the uploader's other
    changes (``services/activity_feed.py``).
    """
    change_feed.record_for(session, actor_id, "songs", change_feed.ADDED, facts)


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


FeedbackDelivery = Literal["emailed", "retrying", "unconfigured"]
"""What became of a piece of feedback's email when it was accepted."""

_DELIVERY_LINES: dict[str, str] = {
    "emailed": "emailed to the maintainer",
    "retrying": "saved; the email failed and will be retried",
    "unconfigured": "saved, not emailed: no Brevo key or feedback address is set",
}


def announce_feedback(
    kind: str, subject: str, *, delivery: FeedbackDelivery, screenshot: bool
) -> None:
    """Post "feedback" to activity when someone sends site feedback.

    The type and subject only — never the message, the sender's name or
    their email: the route is public, the channel is shared, and the whole
    message is kept in ``site_feedback`` and goes by email. Says what became
    of the email. Fire and forget: the person sending feedback does not
    wait on Discord.
    """
    lines = [_md(subject)]
    if screenshot:
        lines.append("with a screenshot")
    lines.append(_DELIVERY_LINES[delivery])
    activity.dispatch(
        send(
            discord.CHANNEL_ACTIVITY,
            {
                "embeds": [
                    {
                        "title": f"{discord.environment_prefix()}feedback · {kind}",
                        "color": _ANNOUNCE_COLOR,
                        "description": "\n".join(lines),
                        "footer": {"text": footer()},
                    }
                ],
                "allowed_mentions": dict(discord.NO_MENTIONS),
            },
            "feedback",
        )
    )
