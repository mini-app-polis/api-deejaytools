-- Migration 016: the activity feed's outbox
--
-- activity_feed: one row per change a person made, written in the same
-- transaction as the change itself (services/change_feed.py), so a change
-- that rolls back leaves no row. The scheduler posts each person's rows to
-- Discord as one message once they have been quiet for a minute, whichever
-- worker or replica the requests landed on. Each uvicorn worker used to
-- gather its own requests in memory, so one person's burst arrived as one
-- message or several depending on where their requests happened to land.
--
-- facts holds what the message needs to say (names, titles, divisions, the
-- fields an edit changed), taken when the change was made, so a row that is
-- deleted can still be described. Never an email, a note or a message body:
-- the channel is a chat room. Posted rows are pruned after a week.
--
-- Additive only. Idempotent: safe to run again.

CREATE TABLE IF NOT EXISTS activity_feed (
  id          BIGSERIAL PRIMARY KEY,
  -- The users.id of the person who made the change. Not a FK: the row
  -- outlives nothing, and a deleted user's last changes still post.
  actor_id    TEXT NOT NULL,
  -- One request's rows share it, so a cascade can be told from its cause.
  -- NULL for work with no request (a song build).
  request_id  TEXT,
  table_name  TEXT NOT NULL,
  -- created | updated | deleted | bulk | added (a song build) | summary
  op          TEXT NOT NULL,
  facts       JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at  BIGINT NOT NULL,
  posted_at   BIGINT
);

CREATE INDEX IF NOT EXISTS idx_activity_feed_unposted
  ON activity_feed (actor_id, created_at) WHERE posted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_activity_feed_posted
  ON activity_feed (posted_at) WHERE posted_at IS NOT NULL;
