-- Background work and per-provider health.
--
-- Jobs are rows rather than in-memory tasks so that a restart can tell the
-- difference between work that finished, work that failed, and work that was
-- cut off midway. That last case gets its own state: a claim killed by a
-- restart may already have spent money, so reporting it as a plain failure
-- would invite a user to retry a purchase they have already made.

CREATE TABLE jobs(
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  kind         TEXT    NOT NULL,                      -- claim | poll | backfill | rescan
  state        TEXT    NOT NULL DEFAULT 'queued',     -- queued | running | finished | failed | interrupted
  track_id     INTEGER REFERENCES tracks(id) ON DELETE CASCADE,
  provider_id  TEXT,
  phase        TEXT,                                  -- session | enumerate | match | download | verify
  progress     TEXT,                                  -- JSON, phase-specific
  error_code   TEXT,
  error        TEXT,
  attempts     INTEGER NOT NULL DEFAULT 0,
  created_at   INTEGER NOT NULL,
  started_at   INTEGER,
  finished_at  INTEGER
);

CREATE INDEX jobs_pending  ON jobs(state, created_at) WHERE state IN ('queued', 'running');
CREATE INDEX jobs_by_track ON jobs(track_id);

-- One row per configured provider, holding what the interface needs to explain
-- itself: whether the provider works, and how long ago that was established.
CREATE TABLE provider_state(
  kind        TEXT    NOT NULL,                       -- source | store
  provider_id TEXT    NOT NULL,
  state       TEXT    NOT NULL DEFAULT 'ok',          -- ok | config | auth_expired | error
  detail      TEXT,
  -- Set when the last check was inconclusive rather than negative. The
  -- interface says "unverified for Nh" for this, which is a different claim
  -- from "working" and from "broken".
  stale       INTEGER NOT NULL DEFAULT 0,
  updated_at  INTEGER NOT NULL,
  PRIMARY KEY (kind, provider_id)
);

-- History, for the status page and for working out whether a provider is
-- failing consistently or intermittently.
CREATE TABLE provider_runs(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT    NOT NULL,
  provider_id TEXT    NOT NULL,
  started_at  INTEGER NOT NULL,
  finished_at INTEGER,
  ok          INTEGER,
  items       INTEGER NOT NULL DEFAULT 0,
  skipped     INTEGER NOT NULL DEFAULT 0,
  error_code  TEXT,
  error       TEXT
);

CREATE INDEX provider_runs_recent ON provider_runs(kind, provider_id, started_at DESC);
