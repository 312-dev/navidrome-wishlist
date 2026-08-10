-- The schema the original application created, reproduced exactly so that a
-- fresh install and an adopted one converge on the same shape.
--
-- This file never executes against an existing installation. The migration
-- runner recognises the legacy schema and stamps user_version to 1 instead. It
-- runs only on an empty database.
--
-- The columns kept here that later migrations replace (dedup_key,
-- source_platform, bandcamp_url, qobuz_url) are dropped in 0007, one release
-- after their readers are gone.

CREATE TABLE tracks(
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  dedup_key       TEXT UNIQUE,
  artist          TEXT,
  title           TEXT,
  source_platform TEXT,
  added_at        INTEGER,
  status          TEXT DEFAULT 'queued',
  resolved        INTEGER DEFAULT 0,
  preview_url     TEXT,
  cover_url       TEXT,
  bandcamp_url    TEXT,
  qobuz_url       TEXT,
  chosen_source   TEXT,
  purchased_at    INTEGER,
  purchased_via   TEXT,
  ignored_at      INTEGER
);
