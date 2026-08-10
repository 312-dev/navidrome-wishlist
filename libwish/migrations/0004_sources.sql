-- Where a loved track came from, and where each source has got to.
--
-- One track can be loved on several services, which a single column on `tracks`
-- cannot say. `track_sources` says it once per service, and its composite
-- primary key is what makes re-delivering a love free: the poll boundary is
-- inclusive on purpose, so the same second arrives twice and the second arrival
-- has to be an ignored insert rather than a duplicate row.

CREATE TABLE track_sources(
  source_id      TEXT    NOT NULL,        -- provider id, or an import: tag (see below)
  source_item_id TEXT    NOT NULL,        -- stable id of THIS love within that source
  track_id       INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  loved_at       INTEGER,                 -- epoch seconds, NULL if the source has none
  first_seen_at  INTEGER NOT NULL,
  last_seen_at   INTEGER NOT NULL,
  raw            TEXT,                    -- the provider's own record, bounded, for diagnosis
  PRIMARY KEY (source_id, source_item_id)
);

CREATE INDEX track_sources_by_track ON track_sources(track_id);

-- One row per configured source. The cursor lives here rather than in a file
-- beside the queue because it has to be written in the same breath as the rows
-- it covers: a cursor recorded before the inserts, which is what the original
-- poller did, loses every love in the window if anything fails in between.
--
-- The cursor is one opaque JSON blob owned by its provider and read by nobody
-- else. It moves forward only.
CREATE TABLE source_state(
  source_id            TEXT PRIMARY KEY,
  enabled              INTEGER NOT NULL DEFAULT 0,
  mode                 TEXT    NOT NULL DEFAULT 'incremental',
                                          -- seed | incremental | backfill | paused
  cursor               TEXT,               -- JSON, provider-owned, opaque here

  health               TEXT    NOT NULL DEFAULT 'unconfigured',
                                          -- ok | unconfigured | needs_auth | rate_limited
                                          -- | degraded | error | paused
  last_ok_at           INTEGER,
  last_attempt_at      INTEGER,
  last_error           TEXT,
  last_error_kind      TEXT,
  last_error_at        INTEGER,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  next_due_at          INTEGER NOT NULL DEFAULT 0,

  items_seen           INTEGER NOT NULL DEFAULT 0,
  items_added          INTEGER NOT NULL DEFAULT 0
);

-- Provenance for the rows that predate this table.
--
-- `tracks.source_platform` holds three values in the live database: 'lastfm' (6
-- rows), 'listenbrainz' (2) and 'deezer-unobtainable' (156). The first two name
-- a source. The third does not: it is a note left by a retired job recording
-- that the track could not be obtained from Deezer, and it says nothing about
-- where the love came from.
--
-- So it keeps its own name under an `import:` prefix. The prefix cannot collide
-- with a provider id, because a provider id may not contain a colon, and that
-- is the point: an `import:` row is provenance only. It is never polled, never
-- appears in the sources list, and no code will ever look for a provider to
-- serve it. Inventing a `deezer` source to hold these rows would have claimed
-- something untrue about all 156 of them and would have burned an id that a
-- real Deezer source will want.
--
-- No original per-love id survives, so one is synthesized from the track id. It
-- is stable, which is all the primary key needs it to be.
INSERT INTO track_sources(source_id, source_item_id, track_id,
                          loved_at, first_seen_at, last_seen_at)
SELECT
  CASE source_platform
    WHEN 'lastfm'       THEN 'lastfm'
    WHEN 'listenbrainz' THEN 'listenbrainz'
    ELSE 'import:' || source_platform
  END,
  'legacy:' || id,
  id,
  added_at,
  COALESCE(added_at, 0),
  COALESCE(added_at, 0)
FROM tracks
WHERE source_platform IS NOT NULL AND TRIM(source_platform) <> '';

-- `tracks.source_platform` stays for one release so that anything still reading
-- it keeps working, and is dropped in 0007 with the rest of the old columns.
