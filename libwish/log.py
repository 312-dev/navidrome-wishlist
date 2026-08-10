"""Logging with bound context.

Every provider gets a logger already carrying its own id, so a line in the log
identifies which provider produced it without each call site having to remember
to say so. Output is plain text by default because a person reads it in
`docker logs`, and JSON when `LW_LOG_JSON` is set for anything shipping to a log
aggregator.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "context"):
            record.context = {}
        return True


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        ctx: dict[str, Any] = getattr(record, "context", {}) or {}
        if not ctx:
            return base
        return base + " " + " ".join(f"{k}={v}" for k, v in sorted(ctx.items()))


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(getattr(record, "context", {}) or {})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class Logger:
    """A logging facade that carries context forward through `bind`."""

    def __init__(self, inner: logging.Logger, context: dict[str, Any] | None = None) -> None:
        self._inner = inner
        self._context = dict(context or {})

    def bind(self, **kw: Any) -> "Logger":
        return Logger(self._inner, {**self._context, **kw})

    def _log(self, level: int, msg: str, *args: Any, **kw: Any) -> None:
        extra = {"context": {**self._context, **kw.pop("context", {})}}
        self._inner.log(level, msg, *args, extra=extra, **kw)

    def debug(self, msg: str, *a: Any, **kw: Any) -> None: self._log(logging.DEBUG, msg, *a, **kw)
    def info(self, msg: str, *a: Any, **kw: Any) -> None: self._log(logging.INFO, msg, *a, **kw)
    def warning(self, msg: str, *a: Any, **kw: Any) -> None: self._log(logging.WARNING, msg, *a, **kw)
    def error(self, msg: str, *a: Any, **kw: Any) -> None: self._log(logging.ERROR, msg, *a, **kw)

    def exception(self, msg: str, *a: Any, **kw: Any) -> None:
        self._log(logging.ERROR, msg, *a, exc_info=True, **kw)


_configured = False


def configure(level: str = "INFO", as_json: bool = False) -> None:
    global _configured
    root = logging.getLogger("libwish")
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_ContextFilter())
    handler.setFormatter(
        _JsonFormatter() if as_json
        else _TextFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    )
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))
    root.propagate = False
    _configured = True


def get(name: str = "libwish", **context: Any) -> Logger:
    if not _configured:
        configure()
    full = name if name.startswith("libwish") else f"libwish.{name}"
    return Logger(logging.getLogger(full), context)
