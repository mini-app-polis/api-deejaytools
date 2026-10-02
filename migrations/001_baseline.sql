-- Migration 001: baseline
--
-- The deejaytools schema exactly as deejaytools-api's drizzle migrations
-- 0000-0015 leave it, enums, constraints, indexes and recorded drift
-- included (deejaytools-api ADR-008 point 2). Generated, never edited:
--
--   REGENERATE=1 scripts/check_baseline.sh
--
-- Production adopts this file without running it. The first deploy of this
-- service sets BOOTSTRAP_MIGRATIONS=true and BOOTSTRAP_EXCLUDE naming every
-- later file, so the runner records 001 as applied and runs the rest.
-- Test databases are built from it, and CI checks it against drizzle's
-- result on every run (scripts/check_baseline.sh).
--
-- Only the `public` schema. drizzle's own `drizzle` schema and its journal
-- stay in production untouched until deejaytools-api is retired.


CREATE TYPE "public"."initial_queue" AS ENUM (
    'priority',
    'non_priority'
);

CREATE TYPE "public"."partner_role" AS ENUM (
    'leader',
    'follower'
);

CREATE TYPE "public"."queue_event_action" AS ENUM (
    'checked_in',
    'promoted_to_active',
    'run_completed',
    'run_incomplete_rotated',
    'withdrawn',
    'moved_within_queue'
);

CREATE TYPE "public"."queue_type" AS ENUM (
    'priority',
    'non_priority',
    'active'
);

CREATE TYPE "public"."session_status" AS ENUM (
    'scheduled',
    'checkin_open',
    'in_progress',
    'completed',
    'cancelled'
);

CREATE TYPE "public"."user_role" AS ENUM (
    'user',
    'admin'
);

CREATE TABLE "public"."checkins" (
    "id" "text" NOT NULL,
    "session_id" "text" NOT NULL,
    "division_name" "text" NOT NULL,
    "entity_pair_id" "text",
    "entity_solo_user_id" "text",
    "song_id" "text" NOT NULL,
    "submitted_by_user_id" "text" NOT NULL,
    "initial_queue" "public"."initial_queue" NOT NULL,
    "notes" "text",
    "created_at" bigint NOT NULL,
    "entity_managed_partnership_id" "text",
    CONSTRAINT "ck_checkins_entity_xor" CHECK (((("entity_pair_id" IS NOT NULL) AND ("entity_solo_user_id" IS NULL) AND ("entity_managed_partnership_id" IS NULL)) OR (("entity_pair_id" IS NULL) AND ("entity_solo_user_id" IS NOT NULL) AND ("entity_managed_partnership_id" IS NULL)) OR (("entity_pair_id" IS NULL) AND ("entity_solo_user_id" IS NULL) AND ("entity_managed_partnership_id" IS NOT NULL))))
);

CREATE TABLE "public"."drive_jobs" (
    "id" "text" NOT NULL,
    "kind" "text" NOT NULL,
    "submission_id" "text",
    "file_id" "text",
    "status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "attempts" integer DEFAULT 0 NOT NULL,
    "next_attempt_at" bigint NOT NULL,
    "last_error" "text",
    "created_at" bigint NOT NULL,
    "updated_at" bigint NOT NULL
);

CREATE TABLE "public"."event_division_run_limits" (
    "event_id" "text" NOT NULL,
    "division_name" "text" NOT NULL,
    "priority_run_limit" integer NOT NULL
);

CREATE TABLE "public"."event_song_submissions" (
    "id" "text" NOT NULL,
    "event_id" "text" NOT NULL,
    "song_id" "text" NOT NULL,
    "submitted_by_user_id" "text" NOT NULL,
    "created_at" bigint NOT NULL,
    "drive_copy_file_id" "text",
    "division" "text",
    "round" "text"
);

CREATE TABLE "public"."events" (
    "id" "text" NOT NULL,
    "name" "text" NOT NULL,
    "created_by" "text",
    "created_at" bigint NOT NULL,
    "updated_at" bigint NOT NULL,
    "start_date" "text" NOT NULL,
    "end_date" "text" NOT NULL,
    "timezone" "text" DEFAULT 'America/Chicago'::"text" NOT NULL,
    "season_year" "text",
    CONSTRAINT "ck_events_date_range" CHECK (("start_date" <= "end_date"))
);

CREATE TABLE "public"."legacy_songs" (
    "id" "text" NOT NULL,
    "partnership" "text" NOT NULL,
    "division" "text",
    "routine_name" "text",
    "descriptor" "text",
    "version" "text",
    "submitted_at" "text",
    "created_at" bigint NOT NULL
);

CREATE TABLE "public"."managed_partnerships" (
    "id" "text" NOT NULL,
    "user_id" "text" NOT NULL,
    "leader_first_name" "text" NOT NULL,
    "leader_last_name" "text" NOT NULL,
    "follower_first_name" "text" NOT NULL,
    "follower_last_name" "text" NOT NULL,
    "created_at" bigint NOT NULL,
    "updated_at" bigint NOT NULL,
    "deleted_at" bigint
);

CREATE TABLE "public"."pairs" (
    "id" "text" NOT NULL,
    "user_a_id" "text" NOT NULL,
    "partner_b_id" "text",
    "created_at" bigint NOT NULL
);

CREATE TABLE "public"."partners" (
    "id" "text" NOT NULL,
    "user_id" "text" NOT NULL,
    "first_name" "text" NOT NULL,
    "last_name" "text" NOT NULL,
    "email" "text",
    "linked_user_id" "text",
    "created_at" bigint NOT NULL,
    "updated_at" bigint NOT NULL,
    "partner_role" "public"."partner_role" DEFAULT 'follower'::"public"."partner_role" NOT NULL,
    "kind" "text" DEFAULT 'partner'::"text" NOT NULL
);

CREATE TABLE "public"."queue_entries" (
    "id" "text" NOT NULL,
    "checkin_id" "text" NOT NULL,
    "session_id" "text" NOT NULL,
    "entity_pair_id" "text",
    "entity_solo_user_id" "text",
    "queue_type" "public"."queue_type" NOT NULL,
    "position" integer NOT NULL,
    "entered_queue_at" bigint NOT NULL,
    "entity_managed_partnership_id" "text",
    CONSTRAINT "ck_queue_entries_entity_xor" CHECK (((("entity_pair_id" IS NOT NULL) AND ("entity_solo_user_id" IS NULL) AND ("entity_managed_partnership_id" IS NULL)) OR (("entity_pair_id" IS NULL) AND ("entity_solo_user_id" IS NOT NULL) AND ("entity_managed_partnership_id" IS NULL)) OR (("entity_pair_id" IS NULL) AND ("entity_solo_user_id" IS NULL) AND ("entity_managed_partnership_id" IS NOT NULL)))),
    CONSTRAINT "ck_queue_entries_position_positive" CHECK (("position" >= 1))
);

CREATE TABLE "public"."queue_events" (
    "id" "text" NOT NULL,
    "session_id" "text" NOT NULL,
    "checkin_id" "text",
    "action" "public"."queue_event_action" NOT NULL,
    "from_queue" "public"."queue_type",
    "from_position" integer,
    "to_queue" "public"."queue_type",
    "to_position" integer,
    "actor_user_id" "text",
    "reason" "text",
    "created_at" bigint NOT NULL
);

CREATE TABLE "public"."runs" (
    "id" "text" NOT NULL,
    "checkin_id" "text" NOT NULL,
    "session_id" "text" NOT NULL,
    "event_id" "text",
    "division_name" "text" NOT NULL,
    "entity_pair_id" "text",
    "entity_solo_user_id" "text",
    "song_id" "text" NOT NULL,
    "completed_at" bigint NOT NULL,
    "completed_by_user_id" "text" NOT NULL,
    "entity_managed_partnership_id" "text",
    CONSTRAINT "ck_runs_entity_xor" CHECK (((("entity_pair_id" IS NOT NULL) AND ("entity_solo_user_id" IS NULL) AND ("entity_managed_partnership_id" IS NULL)) OR (("entity_pair_id" IS NULL) AND ("entity_solo_user_id" IS NOT NULL) AND ("entity_managed_partnership_id" IS NULL)) OR (("entity_pair_id" IS NULL) AND ("entity_solo_user_id" IS NULL) AND ("entity_managed_partnership_id" IS NOT NULL))))
);

CREATE TABLE "public"."session_divisions" (
    "id" "text" NOT NULL,
    "session_id" "text" NOT NULL,
    "division_name" "text" NOT NULL,
    "is_priority" boolean DEFAULT false NOT NULL,
    "sort_order" integer DEFAULT 0 NOT NULL,
    "priority_run_limit" integer DEFAULT 0 NOT NULL
);

CREATE TABLE "public"."sessions" (
    "id" "text" NOT NULL,
    "event_id" "text",
    "name" "text" NOT NULL,
    "date" "text",
    "checkin_opens_at" bigint NOT NULL,
    "floor_trial_starts_at" bigint NOT NULL,
    "floor_trial_ends_at" bigint NOT NULL,
    "status" "public"."session_status" DEFAULT 'scheduled'::"public"."session_status" NOT NULL,
    "created_by" "text",
    "created_at" bigint NOT NULL,
    "active_priority_max" integer DEFAULT 6 NOT NULL,
    "active_non_priority_max" integer DEFAULT 4 NOT NULL,
    CONSTRAINT "ck_sessions_active_caps" CHECK ((("active_non_priority_max" <= "active_priority_max") AND ("active_priority_max" >= 0)))
);

CREATE TABLE "public"."songs" (
    "id" "text" NOT NULL,
    "user_id" "text" NOT NULL,
    "partner_id" "text",
    "display_name" "text",
    "original_filename" "text",
    "drive_file_id" "text",
    "drive_folder_id" "text",
    "processed_filename" "text",
    "division" "text",
    "routine_name" "text",
    "personal_descriptor" "text",
    "season_year" "text",
    "created_at" bigint NOT NULL,
    "updated_at" bigint NOT NULL,
    "deleted_at" bigint,
    "managed_partnership_id" "text"
);

CREATE TABLE "public"."teams" (
    "id" "text" NOT NULL,
    "user_id" "text" NOT NULL,
    "identifier" "text" NOT NULL,
    "created_at" bigint NOT NULL,
    "updated_at" bigint NOT NULL
);

CREATE TABLE "public"."users" (
    "id" "text" NOT NULL,
    "email" "text" NOT NULL,
    "display_name" "text",
    "first_name" "text",
    "last_name" "text",
    "role" "public"."user_role" DEFAULT 'user'::"public"."user_role" NOT NULL,
    "created_at" bigint NOT NULL,
    "updated_at" bigint NOT NULL
);

ALTER TABLE ONLY "public"."checkins"
    ADD CONSTRAINT "checkins_pkey" PRIMARY KEY ("id");

ALTER TABLE ONLY "public"."drive_jobs"
    ADD CONSTRAINT "drive_jobs_pkey" PRIMARY KEY ("id");

ALTER TABLE ONLY "public"."event_song_submissions"
    ADD CONSTRAINT "event_song_submissions_pkey" PRIMARY KEY ("id");

ALTER TABLE ONLY "public"."events"
    ADD CONSTRAINT "events_pkey" PRIMARY KEY ("id");

ALTER TABLE ONLY "public"."legacy_songs"
    ADD CONSTRAINT "legacy_songs_pkey" PRIMARY KEY ("id");

ALTER TABLE ONLY "public"."managed_partnerships"
    ADD CONSTRAINT "managed_partnerships_pkey" PRIMARY KEY ("id");

ALTER TABLE ONLY "public"."pairs"
    ADD CONSTRAINT "pairs_pkey" PRIMARY KEY ("id");

ALTER TABLE ONLY "public"."partners"
    ADD CONSTRAINT "partners_pkey" PRIMARY KEY ("id");

ALTER TABLE ONLY "public"."queue_entries"
    ADD CONSTRAINT "queue_entries_checkin_id_unique" UNIQUE ("checkin_id");

ALTER TABLE ONLY "public"."queue_entries"
    ADD CONSTRAINT "queue_entries_pkey" PRIMARY KEY ("id");

ALTER TABLE ONLY "public"."queue_events"
    ADD CONSTRAINT "queue_events_pkey" PRIMARY KEY ("id");

ALTER TABLE ONLY "public"."runs"
    ADD CONSTRAINT "runs_checkin_id_unique" UNIQUE ("checkin_id");

ALTER TABLE ONLY "public"."runs"
    ADD CONSTRAINT "runs_pkey" PRIMARY KEY ("id");

ALTER TABLE ONLY "public"."session_divisions"
    ADD CONSTRAINT "session_divisions_pkey" PRIMARY KEY ("id");

ALTER TABLE ONLY "public"."sessions"
    ADD CONSTRAINT "sessions_pkey" PRIMARY KEY ("id");

ALTER TABLE ONLY "public"."songs"
    ADD CONSTRAINT "songs_pkey" PRIMARY KEY ("id");

ALTER TABLE ONLY "public"."teams"
    ADD CONSTRAINT "teams_pkey" PRIMARY KEY ("id");

ALTER TABLE ONLY "public"."session_divisions"
    ADD CONSTRAINT "uq_session_divisions_session_division" UNIQUE ("session_id", "division_name");

ALTER TABLE ONLY "public"."users"
    ADD CONSTRAINT "users_email_unique" UNIQUE ("email");

ALTER TABLE ONLY "public"."users"
    ADD CONSTRAINT "users_pkey" PRIMARY KEY ("id");

CREATE INDEX "idx_checkins_entity_managed_partnership_id" ON "public"."checkins" USING "btree" ("entity_managed_partnership_id");

CREATE INDEX "idx_checkins_entity_pair_id" ON "public"."checkins" USING "btree" ("entity_pair_id");

CREATE INDEX "idx_checkins_entity_solo_user_id" ON "public"."checkins" USING "btree" ("entity_solo_user_id");

CREATE INDEX "idx_checkins_session_id" ON "public"."checkins" USING "btree" ("session_id");

CREATE INDEX "idx_drive_jobs_due" ON "public"."drive_jobs" USING "btree" ("status", "next_attempt_at");

CREATE INDEX "idx_drive_jobs_submission_id" ON "public"."drive_jobs" USING "btree" ("submission_id");

CREATE INDEX "idx_event_song_submissions_event_id" ON "public"."event_song_submissions" USING "btree" ("event_id");

CREATE INDEX "idx_event_song_submissions_submitted_by_user_id" ON "public"."event_song_submissions" USING "btree" ("submitted_by_user_id");

CREATE INDEX "idx_legacy_songs_division" ON "public"."legacy_songs" USING "btree" ("division");

CREATE INDEX "idx_managed_partnerships_user_id" ON "public"."managed_partnerships" USING "btree" ("user_id");

CREATE INDEX "idx_partners_email" ON "public"."partners" USING "btree" ("email");

CREATE INDEX "idx_partners_linked_user_id" ON "public"."partners" USING "btree" ("linked_user_id");

CREATE INDEX "idx_partners_user_id" ON "public"."partners" USING "btree" ("user_id");

CREATE INDEX "idx_queue_entries_session_id" ON "public"."queue_entries" USING "btree" ("session_id");

CREATE INDEX "idx_queue_events_session_created" ON "public"."queue_events" USING "btree" ("session_id", "created_at");

CREATE INDEX "idx_runs_event_id" ON "public"."runs" USING "btree" ("event_id");

CREATE INDEX "idx_runs_managed_division" ON "public"."runs" USING "btree" ("entity_managed_partnership_id", "division_name");

CREATE INDEX "idx_runs_pair_division" ON "public"."runs" USING "btree" ("entity_pair_id", "division_name");

CREATE INDEX "idx_runs_session_id" ON "public"."runs" USING "btree" ("session_id");

CREATE INDEX "idx_runs_solo_division" ON "public"."runs" USING "btree" ("entity_solo_user_id", "division_name");

CREATE INDEX "idx_sessions_event_id" ON "public"."sessions" USING "btree" ("event_id");

CREATE INDEX "idx_songs_user_id" ON "public"."songs" USING "btree" ("user_id");

CREATE INDEX "idx_teams_user_id" ON "public"."teams" USING "btree" ("user_id");

CREATE UNIQUE INDEX "uq_event_division_run_limits_pk" ON "public"."event_division_run_limits" USING "btree" ("event_id", "division_name");

CREATE UNIQUE INDEX "uq_event_song_submissions_event_song" ON "public"."event_song_submissions" USING "btree" ("event_id", "song_id");

CREATE UNIQUE INDEX "uq_pairs_user_partner" ON "public"."pairs" USING "btree" ("user_a_id", "partner_b_id");

CREATE UNIQUE INDEX "uq_queue_entries_session_managed_live" ON "public"."queue_entries" USING "btree" ("session_id", "entity_managed_partnership_id") WHERE ("entity_managed_partnership_id" IS NOT NULL);

CREATE UNIQUE INDEX "uq_queue_entries_session_pair_live" ON "public"."queue_entries" USING "btree" ("session_id", "entity_pair_id") WHERE ("entity_pair_id" IS NOT NULL);

CREATE UNIQUE INDEX "uq_queue_entries_session_queue_position" ON "public"."queue_entries" USING "btree" ("session_id", "queue_type", "position");

CREATE UNIQUE INDEX "uq_queue_entries_session_solo_live" ON "public"."queue_entries" USING "btree" ("session_id", "entity_solo_user_id") WHERE ("entity_solo_user_id" IS NOT NULL);

CREATE UNIQUE INDEX "uq_teams_user_identifier" ON "public"."teams" USING "btree" ("user_id", "identifier");

ALTER TABLE ONLY "public"."checkins"
    ADD CONSTRAINT "checkins_entity_managed_partnership_id_managed_partnerships_id_" FOREIGN KEY ("entity_managed_partnership_id") REFERENCES "public"."managed_partnerships"("id") ON DELETE RESTRICT;

ALTER TABLE ONLY "public"."checkins"
    ADD CONSTRAINT "checkins_entity_pair_id_pairs_id_fk" FOREIGN KEY ("entity_pair_id") REFERENCES "public"."pairs"("id") ON DELETE RESTRICT;

ALTER TABLE ONLY "public"."checkins"
    ADD CONSTRAINT "checkins_entity_solo_user_id_users_id_fk" FOREIGN KEY ("entity_solo_user_id") REFERENCES "public"."users"("id") ON DELETE RESTRICT;

ALTER TABLE ONLY "public"."checkins"
    ADD CONSTRAINT "checkins_session_id_sessions_id_fk" FOREIGN KEY ("session_id") REFERENCES "public"."sessions"("id") ON DELETE RESTRICT;

ALTER TABLE ONLY "public"."checkins"
    ADD CONSTRAINT "checkins_song_id_songs_id_fk" FOREIGN KEY ("song_id") REFERENCES "public"."songs"("id") ON DELETE RESTRICT;

ALTER TABLE ONLY "public"."checkins"
    ADD CONSTRAINT "checkins_submitted_by_user_id_users_id_fk" FOREIGN KEY ("submitted_by_user_id") REFERENCES "public"."users"("id");

ALTER TABLE ONLY "public"."event_division_run_limits"
    ADD CONSTRAINT "event_division_run_limits_event_id_events_id_fk" FOREIGN KEY ("event_id") REFERENCES "public"."events"("id");

ALTER TABLE ONLY "public"."event_song_submissions"
    ADD CONSTRAINT "event_song_submissions_event_id_events_id_fk" FOREIGN KEY ("event_id") REFERENCES "public"."events"("id");

ALTER TABLE ONLY "public"."event_song_submissions"
    ADD CONSTRAINT "event_song_submissions_song_id_songs_id_fk" FOREIGN KEY ("song_id") REFERENCES "public"."songs"("id");

ALTER TABLE ONLY "public"."event_song_submissions"
    ADD CONSTRAINT "event_song_submissions_submitted_by_user_id_users_id_fk" FOREIGN KEY ("submitted_by_user_id") REFERENCES "public"."users"("id");

ALTER TABLE ONLY "public"."events"
    ADD CONSTRAINT "events_created_by_users_id_fk" FOREIGN KEY ("created_by") REFERENCES "public"."users"("id");

ALTER TABLE ONLY "public"."checkins"
    ADD CONSTRAINT "fk_checkins_session_division" FOREIGN KEY ("session_id", "division_name") REFERENCES "public"."session_divisions"("session_id", "division_name") ON DELETE RESTRICT;

ALTER TABLE ONLY "public"."managed_partnerships"
    ADD CONSTRAINT "managed_partnerships_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id");

ALTER TABLE ONLY "public"."pairs"
    ADD CONSTRAINT "pairs_partner_b_id_partners_id_fk" FOREIGN KEY ("partner_b_id") REFERENCES "public"."partners"("id");

ALTER TABLE ONLY "public"."pairs"
    ADD CONSTRAINT "pairs_user_a_id_users_id_fk" FOREIGN KEY ("user_a_id") REFERENCES "public"."users"("id");

ALTER TABLE ONLY "public"."partners"
    ADD CONSTRAINT "partners_linked_user_id_users_id_fk" FOREIGN KEY ("linked_user_id") REFERENCES "public"."users"("id");

ALTER TABLE ONLY "public"."partners"
    ADD CONSTRAINT "partners_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id");

ALTER TABLE ONLY "public"."queue_entries"
    ADD CONSTRAINT "queue_entries_checkin_id_checkins_id_fk" FOREIGN KEY ("checkin_id") REFERENCES "public"."checkins"("id");

ALTER TABLE ONLY "public"."queue_entries"
    ADD CONSTRAINT "queue_entries_entity_managed_partnership_id_managed_partnership" FOREIGN KEY ("entity_managed_partnership_id") REFERENCES "public"."managed_partnerships"("id") ON DELETE RESTRICT;

ALTER TABLE ONLY "public"."queue_entries"
    ADD CONSTRAINT "queue_entries_entity_pair_id_pairs_id_fk" FOREIGN KEY ("entity_pair_id") REFERENCES "public"."pairs"("id") ON DELETE RESTRICT;

ALTER TABLE ONLY "public"."queue_entries"
    ADD CONSTRAINT "queue_entries_entity_solo_user_id_users_id_fk" FOREIGN KEY ("entity_solo_user_id") REFERENCES "public"."users"("id") ON DELETE RESTRICT;

ALTER TABLE ONLY "public"."queue_entries"
    ADD CONSTRAINT "queue_entries_session_id_sessions_id_fk" FOREIGN KEY ("session_id") REFERENCES "public"."sessions"("id");

ALTER TABLE ONLY "public"."queue_events"
    ADD CONSTRAINT "queue_events_actor_user_id_users_id_fk" FOREIGN KEY ("actor_user_id") REFERENCES "public"."users"("id");

ALTER TABLE ONLY "public"."queue_events"
    ADD CONSTRAINT "queue_events_checkin_id_checkins_id_fk" FOREIGN KEY ("checkin_id") REFERENCES "public"."checkins"("id");

ALTER TABLE ONLY "public"."queue_events"
    ADD CONSTRAINT "queue_events_session_id_sessions_id_fk" FOREIGN KEY ("session_id") REFERENCES "public"."sessions"("id");

ALTER TABLE ONLY "public"."runs"
    ADD CONSTRAINT "runs_checkin_id_checkins_id_fk" FOREIGN KEY ("checkin_id") REFERENCES "public"."checkins"("id");

ALTER TABLE ONLY "public"."runs"
    ADD CONSTRAINT "runs_completed_by_user_id_users_id_fk" FOREIGN KEY ("completed_by_user_id") REFERENCES "public"."users"("id");

ALTER TABLE ONLY "public"."runs"
    ADD CONSTRAINT "runs_entity_managed_partnership_id_managed_partnerships_id_fk" FOREIGN KEY ("entity_managed_partnership_id") REFERENCES "public"."managed_partnerships"("id") ON DELETE RESTRICT;

ALTER TABLE ONLY "public"."runs"
    ADD CONSTRAINT "runs_entity_pair_id_pairs_id_fk" FOREIGN KEY ("entity_pair_id") REFERENCES "public"."pairs"("id") ON DELETE RESTRICT;

ALTER TABLE ONLY "public"."runs"
    ADD CONSTRAINT "runs_entity_solo_user_id_users_id_fk" FOREIGN KEY ("entity_solo_user_id") REFERENCES "public"."users"("id") ON DELETE RESTRICT;

ALTER TABLE ONLY "public"."runs"
    ADD CONSTRAINT "runs_event_id_events_id_fk" FOREIGN KEY ("event_id") REFERENCES "public"."events"("id");

ALTER TABLE ONLY "public"."runs"
    ADD CONSTRAINT "runs_session_id_sessions_id_fk" FOREIGN KEY ("session_id") REFERENCES "public"."sessions"("id");

ALTER TABLE ONLY "public"."runs"
    ADD CONSTRAINT "runs_song_id_songs_id_fk" FOREIGN KEY ("song_id") REFERENCES "public"."songs"("id") ON DELETE RESTRICT;

ALTER TABLE ONLY "public"."session_divisions"
    ADD CONSTRAINT "session_divisions_session_id_sessions_id_fk" FOREIGN KEY ("session_id") REFERENCES "public"."sessions"("id");

ALTER TABLE ONLY "public"."sessions"
    ADD CONSTRAINT "sessions_created_by_users_id_fk" FOREIGN KEY ("created_by") REFERENCES "public"."users"("id");

ALTER TABLE ONLY "public"."sessions"
    ADD CONSTRAINT "sessions_event_id_events_id_fk" FOREIGN KEY ("event_id") REFERENCES "public"."events"("id");

ALTER TABLE ONLY "public"."songs"
    ADD CONSTRAINT "songs_managed_partnership_id_managed_partnerships_id_fk" FOREIGN KEY ("managed_partnership_id") REFERENCES "public"."managed_partnerships"("id");

ALTER TABLE ONLY "public"."songs"
    ADD CONSTRAINT "songs_partner_id_partners_id_fk" FOREIGN KEY ("partner_id") REFERENCES "public"."partners"("id");

ALTER TABLE ONLY "public"."songs"
    ADD CONSTRAINT "songs_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id");

ALTER TABLE ONLY "public"."teams"
    ADD CONSTRAINT "teams_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id");

