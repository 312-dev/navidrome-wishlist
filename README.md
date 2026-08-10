# Navidrome Wishlist

Turn the tracks you loved on a streaming service into music you actually own.

You find things on Spotify, Apple Music or whatever you listen through. Those
plays scrobble to Last.fm or ListenBrainz, and the ones you loved sit there as
a list nobody acts on. This watches that list, works out which of them are
missing from your own library, and gives you one page of tracks to go and buy.
When you buy one it fetches the file you paid for and files it where your
music server will find it.

That is the whole idea. Streaming pays fractions of a cent and owns the
catalogue it rents you; buying a record pays the artist properly and leaves you
with a file that keeps working when a licence lapses. Most people who would
rather buy simply lose track of what they meant to buy. This is the list.

## What it does

- **Watches what you loved.** Last.fm and ListenBrainz, polled on a timer.
  Faster while a browser is open on the page, slower when nobody is watching.
- **Skips what you already have.** A track already in your library never
  reaches the list.
- **Shows you where to buy it.** Bandcamp and Qobuz. Where more than one sells
  a track, you pick; it never chooses a shop for you.
- **Files what you bought.** Tell it you bought something and it finds that
  purchase in your account, verifies the file is what it claims to be, and
  writes it into your library. Then it asks your music server to rescan.
- **Refuses when it is not sure.** A download only happens when the purchase
  matches the track you wanted, on artist, title and length. Anything short of
  that is shown to you with the candidate it rejected and the score it gave,
  and nothing is downloaded. From there you can override the score, or point
  at the exact purchase in your account yourself; either way the record says
  it was your call, not the matcher's.

It buys nothing on your behalf and it takes no payment details. You buy the
record, in your own browser, from the shop. This finds it afterwards.

## What it is not

Not a downloader. Every file it fetches is one already sitting in your own
purchase history, retrieved with your own logged-in session. Point it at an
account and it can only ever see what that account bought.

## Running it

```
pip install .
LW_CONFIG_DIR=/path/to/config LW_MUSIC_DIR=/path/to/library python -m libwish serve
```

Or with Docker, which mounts the database at `/config` and your library at
`/music`:

```
docker compose up -d --build
```

Then open it, connect a source, and the list fills in.

### Configuration

Everything is an environment variable, all namespaced `LW_`.

| Variable | Meaning | Default |
|---|---|---|
| `LW_CONFIG_DIR` | Database, cookie jars and the cover cache. | `/config` |
| `LW_MUSIC_DIR` | Your library. Downloads land here. | `/music` |
| `LW_DB_PATH` | Database file, if you want it outside the config directory. | `$LW_CONFIG_DIR/library-wishlist.db` |
| `LW_HOST` / `LW_PORT` | What to listen on. | `0.0.0.0:8080` |
| `LW_POLL_HOT_SECONDS` | How often to check sources while someone is watching. | `30` |
| `LW_POLL_COLD_SECONDS` | How often when nobody is. | `600` |
| `LW_LOG_LEVEL` / `LW_LOG_JSON` | Logging. | `INFO`, off |

Sources and stores take their own settings under `LW_SOURCE_<ID>_*` and
`LW_STORE_<ID>_*`, so `LW_SOURCE_LASTFM_API_KEY` and `LW_SOURCE_LASTFM_USER`
configure the Last.fm source and nothing else can read them.

### Connecting a shop

Bandcamp needs nothing to search and link. Qobuz needs a logged-in session,
because the pages listing what you own are only served to a signed-in browser.

`cookie_broker.py` is how a session gets there without one. A browser extension
posts your cookie jar to `/auth/ingest` once, protected by
`COOKIE_BROKER_TOKEN`, and from then on this process owns keeping it alive: it
absorbs the rotation the site performs on every request it makes, and probes on
a timer so an idle session never ages out. Set `LW_STORE_QOBUZ_JAR_PATH` to
point at an existing jar file if you already have one.

The ingest endpoint only exists when `COOKIE_BROKER_TOKEN` is set. It accepts
live credentials, so it should not be reachable at all unless you meant it to
be.

## Install it on a phone

It ships a manifest and a service worker, so Chrome on Android offers to
install it and it opens without browser chrome.

**That needs HTTPS.** Service workers are refused over plain `http://` to
anything but `localhost`, and without one no browser offers to install. Over
`http://box.local:8080` this is a website and nothing more, which works fine,
it just is not installable. Put it behind whatever already terminates TLS on
your network and the option appears.

Offline it shows its own page saying the server is unreachable. It does not
cache the list: what a wishlist says changes while you are looking at it, and a
stale answer to "do I own this" is worse than no answer.

## Before you expose it

The `/api/*` routes have no authentication. It binds `0.0.0.0` by default,
which is right behind a tailnet or a reverse proxy that authenticates for it,
and wrong on an open port.

## Design notes

`docs/` carries the architecture documents this was built from: the identity
and matching rules, the provider contracts, the runtime, and the design. They
describe why the matcher refuses what it refuses, which is the part worth
reading if you plan to trust it with your money.
