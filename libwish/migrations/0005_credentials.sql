-- Stored credentials, and the short-lived rows an authorization flow needs
-- while it is in progress.
--
-- `secret` is plaintext JSON, and the file mode of the database is what
-- protects it. The application must read every secret unattended on every
-- boot, so any key it could decrypt with would have to be reachable by the
-- same process, and in the default single-container layout that key would sit
-- in the same volume as this file. Encrypting the column would move nothing
-- and would make the exposure harder to see. libwish/credentials.py says the
-- same thing at more length.
--
-- The timestamps sit outside the secret on purpose: the janitor decides what
-- to refresh and what to re-probe by reading them, and an expiry is not
-- secret. `public_meta` holds anything else that is safe on a screenshot, such
-- as the account display name and whether the credential can be refreshed at
-- all.

CREATE TABLE credentials(
  provider             TEXT    PRIMARY KEY,          -- "lastfm", "qobuz", "navidrome"
  method               TEXT    NOT NULL,             -- oauth2 | oauth1a | lastfm_web | token_paste | userpass | cookie_jar | none
  status               TEXT    NOT NULL DEFAULT 'absent',
                                                     -- absent | pending | live | expiring | expired | revoked | error | unknown
  secret               TEXT,                         -- JSON: {"app": {...}, "token": {...}}
  public_meta          TEXT    NOT NULL DEFAULT '{}',-- JSON, nothing secret
  expires_at           INTEGER,                      -- unix seconds; NULL means no known expiry
  -- When the janitor should next act on this row: a refresh before an access
  -- token dies, or the single re-probe a 403 schedules.
  refresh_after        INTEGER,
  last_ok_at           INTEGER,
  last_probe_at        INTEGER,
  last_error           TEXT,
  last_error_at        INTEGER,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  created_at           INTEGER NOT NULL,
  updated_at           INTEGER NOT NULL
);

CREATE INDEX credentials_refresh ON credentials(refresh_after) WHERE refresh_after IS NOT NULL;

-- One row per authorization the user has started and not finished. Single use:
-- the row is deleted when it is read, so pasting the same callback URL twice
-- gets a clean "already used" instead of a second exchange against a code the
-- provider has already burned.
CREATE TABLE auth_pending(
  state         TEXT    PRIMARY KEY,   -- 32 random bytes, urlsafe base64
  provider      TEXT    NOT NULL,
  mode          TEXT    NOT NULL,      -- same_origin | loopback_paste | oob | device | poll
  verifier      TEXT,                  -- PKCE code_verifier
  -- Stored rather than recomputed at exchange time. Providers compare the
  -- redirect_uri sent to /token against the one sent to /authorize byte for
  -- byte, and rebuilding it from config lets an edit made mid-flow produce an
  -- error that blames the provider.
  redirect_uri  TEXT    NOT NULL DEFAULT '',
  poll_interval INTEGER,
  created_at    INTEGER NOT NULL,
  expires_at    INTEGER NOT NULL       -- created_at + 600 unless the flow states its own
);

CREATE INDEX auth_pending_expiry ON auth_pending(expires_at);
