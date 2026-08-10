"""Configuration, read from the environment exactly once at startup.

Everything is namespaced `LW_`. Provider configuration is namespaced further as
`LW_SOURCE_<ID>_<KEY>` and `LW_STORE_<ID>_<KEY>`, and providers receive a lookup
closure bound to their own namespace rather than reading the environment
themselves. That is what stops one provider from reading another's secrets, and
it is why `ProviderContext` carries `conf` instead of letting providers import
`os`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from .errors import ConfigError

PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

ProviderKind = Literal["source", "store"]


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _int(key: str, default: int) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from None


def _bool(key: str, default: bool) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key} must be a boolean, got {raw!r}")


@dataclass(frozen=True)
class Settings:
    config_dir: Path
    music_dir: Path
    db_path: Path
    host: str
    port: int
    log_level: str
    log_json: bool

    # How a newly downloaded file is announced to the music server. Empty means
    # the server watches the directory itself and needs no prompting.
    rescan_cmd: str

    # Two polling tiers, not three. The hot interval applies while a browser is
    # connected to the event stream; everything else runs cold. Sources are
    # polled rather than pushed to because neither Last.fm nor ListenBrainz
    # offers a push mechanism.
    poll_hot_seconds: int
    poll_cold_seconds: int
    # A reader who reloads the page should not drop the app back to the cold
    # tier for the length of a reconnect, so presence outlives the connection
    # by this much.
    presence_grace_seconds: int

    http_timeout_seconds: int
    http_user_agent: str

    @property
    def staging_dir(self) -> Path:
        """Where downloads land before they are verified and published.

        Inside the music volume on purpose: publishing has to be a rename, and a
        rename is only atomic within one filesystem. A staging directory on a
        different device turns every publish into a copy, which can be seen
        half-finished by a scanner watching the library.
        """
        return self.music_dir / ".libwish-incoming"

    @classmethod
    def from_env(cls) -> "Settings":
        config_dir = Path(_env("LW_CONFIG_DIR", "/config"))
        music_dir = Path(_env("LW_MUSIC_DIR", "/music"))
        db_path = Path(_env("LW_DB_PATH") or config_dir / "library-wishlist.db")
        port = _int("LW_PORT", 8080)
        if not 1 <= port <= 65535:
            raise ConfigError(f"LW_PORT out of range: {port}")
        return cls(
            config_dir=config_dir,
            music_dir=music_dir,
            db_path=db_path,
            host=_env("LW_HOST", "0.0.0.0"),
            port=port,
            log_level=_env("LW_LOG_LEVEL", "INFO").upper(),
            log_json=_bool("LW_LOG_JSON", False),
            rescan_cmd=os.environ.get("LW_RESCAN_CMD", ""),
            poll_hot_seconds=_int("LW_POLL_HOT_SECONDS", 30),
            poll_cold_seconds=_int("LW_POLL_COLD_SECONDS", 600),
            presence_grace_seconds=_int("LW_PRESENCE_GRACE_SECONDS", 90),
            http_timeout_seconds=_int("LW_HTTP_TIMEOUT_SECONDS", 30),
            http_user_agent=_env("LW_USER_AGENT", "library-wishlist/1.0 (+self-hosted)"),
        )

    def ensure_dirs(self) -> None:
        """Create the directories the application writes to.

        Called once at startup so a misconfigured volume fails immediately with a
        path in the message, rather than at the moment of the first download.
        """
        for path in (self.config_dir, self.music_dir, self.staging_dir, self.db_path.parent):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ConfigError(f"cannot create {path}: {exc}") from None
        if self.staging_dir.stat().st_dev != self.music_dir.stat().st_dev:
            raise ConfigError(
                f"{self.staging_dir} and {self.music_dir} are on different filesystems, "
                "so publishing a download could not be atomic"
            )


def provider_env_prefix(kind: ProviderKind, provider_id: str) -> str:
    if not PROVIDER_ID_RE.match(provider_id):
        raise ConfigError(
            f"provider id {provider_id!r} must match {PROVIDER_ID_RE.pattern}; "
            "ids are part of stored state and cannot change once shipped"
        )
    return f"LW_{kind.upper()}_{provider_id.upper()}_"


def provider_conf(kind: ProviderKind, provider_id: str) -> Callable[[str], str | None]:
    """Return the namespaced lookup a provider is given as `ctx.conf`.

    The returned callable can only see keys under this provider's own prefix, so
    a provider cannot read another's configuration even by accident.
    """
    prefix = provider_env_prefix(kind, provider_id)

    def conf(key: str) -> str | None:
        value = os.environ.get(prefix + key.upper())
        return value.strip() if value is not None else None

    return conf


def configured_provider_ids(kind: ProviderKind) -> set[str]:
    """Provider ids that have at least one environment variable set.

    Used at startup to decide which providers to instantiate, so an unconfigured
    provider is simply absent rather than present and permanently failing.
    """
    prefix = f"LW_{kind.upper()}_"
    found: set[str] = set()
    for name in os.environ:
        if not name.startswith(prefix):
            continue
        rest = name[len(prefix):]
        for candidate in (rest.split("_")[0].lower(), rest.lower()):
            if PROVIDER_ID_RE.match(candidate):
                found.add(candidate)
                break
    return found
