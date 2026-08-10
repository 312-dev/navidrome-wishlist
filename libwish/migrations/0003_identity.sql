-- Track identity: what makes two rows the same recording, and the record of
-- every decision that said so.
--
-- Identity is a three-tier ladder held in separate columns rather than one
-- opaque string: a validated MusicBrainz recording MBID, then an ISRC, then a
-- fingerprint derived from the artist and title. Which tier a row is on decides
-- how much its identity is worth, so it is stored rather than inferred.
--
-- Nothing here reads `dedup_key`. The live database holds two incompatible
-- dialects of that column, written by two importers, and a row's dialect is not
-- recoverable from the value; parsing it would inherit whichever set of bugs
-- produced a given row. The keys below are recomputed from `artist` and
-- `title` by identity.recompute_identity_columns, which is pure and offline and
-- runs as a job rather than here.
--
-- The MusicBrainz promotion from tier 3 to tier 1 is also a job. It is a few
-- hundred network calls at one request per second, and a migration runs inside
-- a transaction at startup on a box that may have no route out.

ALTER TABLE tracks ADD COLUMN artist_key    TEXT    NOT NULL DEFAULT '';
ALTER TABLE tracks ADD COLUMN title_key     TEXT    NOT NULL DEFAULT '';
ALTER TABLE tracks ADD COLUMN qualifier_key TEXT    NOT NULL DEFAULT '';  -- sorted version qualifiers, \x1e joined
ALTER TABLE tracks ADD COLUMN fp_key        TEXT;                         -- artist_key \x1f title_key \x1f qualifier_key
ALTER TABLE tracks ADD COLUMN identity_tier TEXT    NOT NULL DEFAULT 'string';  -- mbid | isrc | string
-- Set when folding emptied the artist or the title, as it does for the artist
-- `¥$`. Such a row can still be claimed, but only through a confirmation.
ALTER TABLE tracks ADD COLUMN identity_degraded INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tracks ADD COLUMN duration_ms   INTEGER;
ALTER TABLE tracks ADD COLUMN identity_lookup_after INTEGER;              -- backoff for the tier-1 retry
ALTER TABLE tracks ADD COLUMN match_status  TEXT    NOT NULL DEFAULT 'unmatched';
                                            -- unmatched | needs_confirm | confirmed | no_confident_match
ALTER TABLE tracks ADD COLUMN merged_into   INTEGER REFERENCES tracks(id);

-- An MBID is a hint until MusicBrainz confirms it. Last.fm hands out MBIDs that
-- do not resolve and track MBIDs where a recording MBID is expected, and both
-- are UUIDs, so the shape proves nothing. A row with validated = 0 carries no
-- weight in scoring and exists so the promotion job knows what to look up.
CREATE TABLE track_mbid(
  track_id   INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  mbid       TEXT    NOT NULL,
  kind       TEXT    NOT NULL,                     -- recording | track | unknown
  validated  INTEGER NOT NULL DEFAULT 0,           -- 1 only after a successful MB lookup
  source     TEXT    NOT NULL,                     -- listenbrainz | lastfm | mb-search | lb-mapper | user
  first_seen INTEGER NOT NULL,
  PRIMARY KEY(track_id, mbid)
);

CREATE INDEX ix_track_mbid_lookup ON track_mbid(mbid)
  WHERE validated = 1 AND kind = 'recording';

-- One recording legitimately carries several ISRCs, so this is a set per track
-- and not a column. Equality between two sets is strong evidence; disagreement
-- is evidence of nothing and never disqualifies.
CREATE TABLE track_isrc(
  track_id   INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  isrc       TEXT    NOT NULL,                     -- upper case, 12 chars, checked against the ISRC grammar
  source     TEXT    NOT NULL,                     -- deezer | qobuz | 7digital | musicbrainz | tags
  first_seen INTEGER NOT NULL,
  PRIMARY KEY(track_id, isrc)
);

CREATE INDEX ix_track_isrc_lookup ON track_isrc(isrc);

-- Scoped to unmerged rows so that merging a duplicate frees its key instead of
-- deadlocking on it. Safe to create before the backfill: fp_key stays NULL
-- until the recompute runs, and the recompute leaves it NULL for any row whose
-- fingerprint collides, reporting the collision rather than merging anything.
CREATE UNIQUE INDEX ux_tracks_fp ON tracks(fp_key)
  WHERE fp_key IS NOT NULL AND merged_into IS NULL;

-- Every decision, including every refusal, because most of the diagnostic value
-- is in what was turned down and why: a log of successes cannot answer "why did
-- nothing happen". The candidate payload is stored raw, as the provider sent
-- it, so a later replay can tell whether the store lied or the parser misread,
-- and can re-run today's matcher over yesterday's candidates with no network
-- and no store session.
CREATE TABLE match_decision(
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  track_id        INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  decided_at      INTEGER NOT NULL,
  phase           TEXT    NOT NULL,                -- dedup | enrich | identity | claim | backfill
  provider        TEXT    NOT NULL,                -- qobuz | bandcamp | 7digital | deezer | musicbrainz | lb-mapper
  matcher_version TEXT    NOT NULL,                -- semver of match.py
  lexicon_hash    TEXT    NOT NULL,                -- sha256 of the serialised qualifier lexicon
  outcome         TEXT    NOT NULL,                -- auto | confirm | refused | user_confirmed | user_rejected | user_override
  score           INTEGER,
  gate_failed     TEXT,
  matched_via     TEXT,
  reasons         TEXT    NOT NULL,                -- JSON array of reason codes
  query_json      TEXT    NOT NULL,                -- serialised TrackIdentity of the queue row
  candidate_json  TEXT,                            -- RAW provider payload of the winner, NULL on refusal
  candidates_considered INTEGER NOT NULL,
  chosen_store_id TEXT,
  file_path       TEXT,
  file_sha256     TEXT,
  duration_ms     INTEGER                          -- of the file actually written, when known
);

CREATE INDEX ix_md_track   ON match_decision(track_id, decided_at DESC);
CREATE INDEX ix_md_outcome ON match_decision(outcome, decided_at DESC);

-- The losers. Without them you cannot tell whether the right item was returned
-- and lost to a better-scoring wrong one, or was never offered at all, and
-- those two failures need different fixes.
CREATE TABLE match_candidate(
  decision_id    INTEGER NOT NULL REFERENCES match_decision(id) ON DELETE CASCADE,
  rank           INTEGER NOT NULL,
  score          INTEGER,
  gate_failed    TEXT,
  reasons        TEXT    NOT NULL,
  candidate_json TEXT    NOT NULL,                 -- RAW provider payload
  PRIMARY KEY(decision_id, rank)
);
