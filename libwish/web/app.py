"""Application construction.

Everything the app needs is built once here and hung off `app.extensions` so that
routes reach their dependencies through the app rather than through module
globals. That is what makes it possible to build a second app against a
throwaway database in a test without the two sharing state.

Startup order matters and is deliberate: migrate before anything reads the
schema, reap interrupted jobs before workers start so a job left running by a
dead process is not picked up as if it were fresh, and sweep staging before
serving so a half-finished download from a previous life cannot be mistaken for
a current one.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable

from flask import Flask

from .. import __version__, db as dbmod
from ..events import EventBus
from ..jobs import JobQueue
from ..log import configure, get
from ..paths import PathService
from ..repo import TrackRepo
from ..settings import Settings

log = get("app")


@dataclass
class Services:
    """The dependency bundle routes reach through `current_app.extensions`."""

    settings: Settings
    db: Callable[[], Any]
    tracks: TrackRepo
    bus: EventBus
    jobs: JobQueue
    paths: PathService
    version: str
    # Attached once providers are wired. Absent means wiring failed, which the
    # status endpoint reports rather than the app refusing to start.
    credentials: Any = None
    stores: dict | None = None
    scheduler: Any = None
    # Live cookie sessions, by provider id. A store whose auth is a browser
    # session reaches its jar through the keeper rather than reading a file, so
    # that a session rotated mid-request is followed rather than overwritten.
    keepers: dict = field(default_factory=dict)


def create_app(settings: Settings | None = None, *, start_workers: bool = True) -> Flask:
    settings = settings or Settings.from_env()
    configure(settings.log_level, settings.log_json)
    settings.ensure_dirs()

    version = dbmod.migrate(settings.db_path)
    log.info("database ready", context={"schema": version, "path": str(settings.db_path)})

    def db_factory():
        return dbmod.connect(settings.db_path)

    bus = EventBus()
    jobs = JobQueue(db_factory, bus)
    paths = PathService(music_dir=settings.music_dir, staging_dir=settings.staging_dir)

    jobs.reap_interrupted()
    swept = paths.sweep_staging()
    if swept:
        log.info("cleared unfinished downloads", context={"files": swept})

    app = Flask(__name__)
    app.extensions["libwish"] = Services(
        settings=settings, db=db_factory, tracks=TrackRepo(db_factory),
        bus=bus, jobs=jobs, paths=paths, version=__version__,
    )

    from .api import bp as api_bp
    app.register_blueprint(api_bp)

    # The rendered pages are optional so that the API can be exercised, and the
    # app can boot, before or without the interface being present.
    try:
        views = importlib.import_module("libwish.web.views")
    except ImportError:
        log.warning("no views module; serving the API only")
    else:
        app.register_blueprint(views.bp)

    _build_keepers(app, settings)
    _register_cookie_broker(app, settings)

    scheduler = _wire_providers(app)
    if start_workers:
        jobs.start(workers=2)
        if scheduler is not None:
            scheduler.start()

    return app


def _wire_providers(app: Flask):
    """Build the configured sources and stores and attach them.

    Failure to build one provider must not stop the application. An unreachable
    or misconfigured store should show as broken in the interface, where a
    person can see it and act, rather than preventing the whole app from
    starting and hiding the reason in a log.
    """
    from ..credentials import ProviderCredentials, SqliteCredentialStore, default_http_factory, harden
    from ..http import HttpClient
    from ..models import ProviderContext
    from ..runtime import wire
    from ..settings import provider_conf

    svc: Services = app.extensions["libwish"]
    creds = SqliteCredentialStore(svc.db, db_path=svc.settings.db_path, logger=get("credentials"))
    harden(svc.settings.db_path)
    http_factory = default_http_factory(svc.settings.http_user_agent,
                                        svc.settings.http_timeout_seconds)

    def context_for(kind: str, provider_id: str) -> ProviderContext:
        return ProviderContext(
            provider_id=provider_id,
            settings=svc.settings,
            log=get(kind, provider=provider_id),
            conf=provider_conf(kind, provider_id),
            # The keeper is what makes a browser session usable without a
            # browser: it holds the jar, absorbs the rotation the site performs
            # on every request, and probes on a timer so an idle session never
            # ages out. Without one here a cookie store sends no cookies and
            # fails at its first request looking like an expired login.
            creds=ProviderCredentials(provider_id, creds, http_factory=http_factory,
                                      keeper=svc.keepers.get(provider_id),
                                      logger=get("credentials", provider=provider_id)),
            http=HttpClient(user_agent=svc.settings.http_user_agent,
                            timeout=svc.settings.http_timeout_seconds,
                            provider_id=provider_id),
            state={},
            db=svc.db,
            paths=svc.paths,
        )

    try:
        scheduler, stores = wire(svc, context_for)
    except Exception as exc:
        log.exception("could not wire providers; serving what already works")
        svc.credentials = creds
        return None
    svc.credentials = creds
    svc.stores = stores
    svc.scheduler = scheduler
    return scheduler


# Sites whose credential is a logged-in browser session rather than a token.
# The probe is a page that is served only to a signed-in visitor and redirects
# to a login screen otherwise, which is what lets a keepalive tell a live
# session from a dead one without parsing anything.
#
# The browser-facing half of each entry is what this app publishes at
# /.well-known/cookie-broker.json for the Cookie Broker extension to install.
# It lives here, beside the probe, so the list of sites this app wants a
# session for is written once. Two lists would drift, and the way they would
# drift is a site being kept alive that the extension was never told to seed.
COOKIE_SITES = {
    "qobuz": {
        "probe_url": "https://www.qobuz.com/profile/downloads/track",
        "jar_env": "LW_STORE_QOBUZ_JAR_PATH",
        "header_env": "LW_STORE_QOBUZ_COOKIE_FILE",
        "jar_name": "qobuz_jar.json",
        "label": "Qobuz",
        "cookie_url": "https://www.qobuz.com/",
        "cookie_domain": "qobuz.com",
        "required_cookie": "qobuz-session",
        "sign_in_url": "https://www.qobuz.com/signin",
    },
}


def _build_keepers(app: Flask, settings: Settings) -> None:
    """Attach a session keeper for every cookie site with a jar to keep.

    Built before the providers so a store that authenticates with a browser
    session is handed the live jar at the moment it is constructed. Skipped
    entirely when cookie_broker is not importable, which leaves those stores
    unauthenticated and visibly broken rather than quietly halfway.

    The jar file is the one the browser extension seeds and the keeper then
    owns. Its format has not changed, so an existing jar is picked up as it
    stands: the path is what has to be pointed at it.
    """
    import os

    svc: Services = app.extensions["libwish"]
    try:
        from cookie_broker import SessionKeeper, SiteConfig
    except ImportError:
        log.warning("cookie_broker is not importable; cookie stores will have no session")
        return

    for site, spec in COOKIE_SITES.items():
        jar = os.environ.get(spec["jar_env"]) or str(settings.config_dir / spec["jar_name"])
        try:
            keeper = SessionKeeper(SiteConfig(
                name=site,
                jar_path=jar,
                probe_url=spec["probe_url"],
                # Kept written for anything outside this process that reads a
                # flat Cookie header. Nothing here does, so it is only set when
                # asked for.
                legacy_header_path=os.environ.get(spec["header_env"]) or None,
            ))
            keeper.start_keepalive()
        except Exception as exc:  # noqa: BLE001 - one bad site must not stop the app
            log.warning("no session keeper for %s: %s", site, exc)
            continue
        svc.keepers[site] = keeper
        log.info("session keeper ready", context={"site": site, "jar": jar})


def _register_cookie_broker(app: Flask, settings: Settings) -> None:
    """Expose the cookie ingest endpoint when a token is configured.

    A browser extension pushes a logged-in cookie jar here and this process then
    owns keeping that session alive. It stays optional because the endpoint
    accepts live credentials, so it should not exist at all unless it has been
    deliberately configured with a token.
    """
    import os

    if not os.environ.get("COOKIE_BROKER_TOKEN"):
        return
    try:
        import cookie_broker
    except ImportError:
        log.warning("COOKIE_BROKER_TOKEN is set but cookie_broker is not importable")
        return
    keepers = getattr(app.extensions["libwish"], "keepers", None) or {}
    try:
        app.register_blueprint(cookie_broker.make_blueprint(keepers))
        log.info("cookie broker mounted", context={"sites": sorted(keepers)})
    except Exception as exc:
        log.warning("cookie broker not mounted: %s", exc)
        return
    _publish_broker_profile(app, sorted(keepers))


def _publish_broker_profile(app: Flask, sites: list[str]) -> None:
    """Advertise what a browser extension should seed, at a fixed address.

    Someone connects this app to the Cookie Broker extension by typing its
    address; the extension reads the document below and asks the browser for
    the permissions it names. Protocol and field meanings are in that project's
    docs/PROTOCOL.md.

    Published only alongside a mounted ingest endpoint, and listing only the
    sites that actually have a keeper. A profile advertising a site this
    process cannot receive would install cleanly and then fail on every push,
    which is a worse failure than not being advertised: the reader would be
    looking for a problem in their browser rather than in this app's
    configuration.

    `receiver.base` is deliberately absent. The extension takes the address it
    fetched this from, so the document stays correct for every deployment
    rather than naming whichever one happened to write it.
    """
    from flask import jsonify

    profile = {
        "protocol": 1,
        "id": "library-wishlist",
        "name": "Library Wishlist",
        "homepage": "https://github.com/312-dev/navidrome-wishlist",
        "receiver": {
            "ingest": "/auth/ingest/{site}",
            "status": "/auth/status/{site}",
            "auth": "bearer",
        },
        # The in-page banner: a bar on a shop's page after a purchase,
        # offering to file it and to go back to the list. Declared here rather
        # than configured in the extension, so connecting this app is one
        # address and nothing else. The extension implements the behaviour and
        # will not accept a capability it does not know; this only says which
        # shops, which URL marker, and which paths on this server.
        "integration": {
            "capability": "purchase-return",
            "marker": "lw",
            "sites": sites,
            "api": {
                "item": "/api/track/{id}",
                "job": "/api/jobs/{id}",
                "claim": "/api/claim/{id}",
                "pick": "/api/claim/{id}/pick",
                "purchases": "/api/purchases/{store}",
            },
        },
        "sites": [
            {
                "id": site,
                "label": COOKIE_SITES[site]["label"],
                "cookieUrl": COOKIE_SITES[site]["cookie_url"],
                "cookieDomain": COOKIE_SITES[site]["cookie_domain"],
                "requiredCookie": COOKIE_SITES[site]["required_cookie"],
                "signInUrl": COOKIE_SITES[site]["sign_in_url"],
            }
            for site in sites
            if site in COOKIE_SITES
        ],
    }

    @app.get("/.well-known/cookie-broker.json")
    def broker_profile():  # noqa: ANN202 - Flask view
        # Unauthenticated on purpose. It carries no secret: it is a list of
        # which shops this app wants a session for, and the address it is
        # being read from is already known to whoever is reading it. Requiring
        # the token here would mean the token had to be typed before the
        # profile that explains what the token is for could be seen.
        return jsonify(profile)

    log.info("broker profile published", context={"sites": sites})


def serve(settings: Settings | None = None) -> None:
    """Run the production server.

    Waitress rather than the development server: the latter warns that it is not
    for production and handles one request at a time, which stalls the page while
    the event stream is open.
    """
    from waitress import serve as waitress_serve

    settings = settings or Settings.from_env()
    app = create_app(settings)
    # One worker is held for the whole life of every open event stream, so the
    # pool has to cover the stream cap plus enough left over to serve ordinary
    # requests. EventBus.MAX_CLIENTS bounds the first term; the remainder is
    # what stops a few open tabs from making the application unreachable.
    from ..events import MAX_CLIENTS

    threads = MAX_CLIENTS + 16
    log.info("listening", context={"host": settings.host, "port": settings.port,
                                   "threads": threads, "stream_cap": MAX_CLIENTS})
    waitress_serve(app, host=settings.host, port=settings.port, threads=threads)
