"""Store providers and their registry.

A store is somewhere the user can buy a track or already owns one. Registration
is explicit rather than discovered by scanning entry points, because the set of
stores is small, and an import that silently fails to register is much harder to
diagnose than a name that is simply absent from this list.
"""

from __future__ import annotations

from typing import Callable, Iterable

from ..errors import ConfigError
from ..log import get
from ..models import ProviderContext, StoreProvider
from ..settings import configured_provider_ids, provider_conf

log = get("stores")

_REGISTRY: dict[str, type] = {}


def register(cls: type) -> type:
    """Add a store class to the registry. Usable as a decorator."""
    store_id = getattr(cls, "id", "")
    if not store_id:
        raise ConfigError(f"{cls.__name__} has no id")
    if store_id in _REGISTRY and _REGISTRY[store_id] is not cls:
        raise ConfigError(f"two stores claim the id {store_id!r}")
    _REGISTRY[store_id] = cls
    return cls


def available() -> dict[str, type]:
    _load_builtins()
    return dict(_REGISTRY)


def _load_builtins() -> None:
    from . import qobuz  # noqa: F401  imported for its registration side effect

    try:
        from . import bandcamp  # noqa: F401
    except ImportError:
        pass


def build(ids: Iterable[str] | None, context_for: Callable[[str], ProviderContext]) -> dict[str, StoreProvider]:
    """Instantiate the stores.

    Every registered store is built unless the caller names a subset, which is
    the opposite of how sources are treated and deliberately so. A source with no
    credential can do nothing but fail on a timer. A store with no credential is
    still useful: `buy_url` needs nothing at all, and some searches work signed
    out, so an install with no configuration should still be able to point you at
    somewhere to buy a track. Each store reports its own auth state through
    `check()`, so what is missing is visible without being fatal.
    """
    known = available()
    wanted = list(ids) if ids is not None else sorted(known)
    built: dict[str, StoreProvider] = {}
    for store_id in wanted:
        cls = known.get(store_id)
        if cls is None:
            log.warning("no such store", context={"store": store_id})
            continue
        built[store_id] = cls(context_for(store_id))
    return built
