-- Migration 004: who uploaded a staged song
--
-- A song added through "Upload For" belongs to the dancer it was uploaded
-- for (songs.user_id) but was uploaded by someone else: an organizer or an
-- admin. The "song added" notification names both, and it is posted when
-- the Drive build succeeds — possibly after a restart, from the scheduler,
-- long after the upload request is gone. So the uploader is staged with
-- the bytes, in this service's own table.
--
-- Additive: one nullable column on song_uploads, which deejaytools-api
-- never reads. NULL (a row staged before this migration) reads as "uploaded
-- by the song's owner".

ALTER TABLE song_uploads ADD COLUMN IF NOT EXISTS uploaded_by_user_id TEXT;
