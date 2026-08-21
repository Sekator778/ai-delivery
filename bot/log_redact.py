"""log_redact.py — logging.Filter that keeps secrets out of bot.py's logs.

Two independent redaction passes run over every formatted log message before
it reaches any handler:

1. Telegram bot tokens embedded in URLs — httpx logs the full request URL at
   INFO level (`POST https://api.telegram.org/bot<TOKEN>/getUpdates`), and
   that URL reaches the logs whenever the root logger level is DEBUG/INFO.
2. The literal VALUE of any environment variable whose NAME looks secret
   shaped (`.*(_KEY|_TOKEN|_SECRET|PASSWORD).*`, case-insensitive) — this
   catches DEEPSEEK_API_KEY, GLM_API_KEY, LITELLM_MASTER_KEY, TELEGRAM_TOKEN,
   BOT_TOKEN, etc. without hard-coding the project's env var names one by
   one, so a newly added secret-shaped var is covered automatically.

Attach ONE `SecretRedactionFilter` to the ROOT logger (see `install()`) so
every logger in the process — bot's own, httpx's, telegram's, asyncio's —
passes through it; nothing needs to remember to filter itself individually.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Mapping, Optional

# bot<digits>:<30+ url-safe chars> — the shape of a Telegram bot API token as
# it appears inside an API URL (https://api.telegram.org/bot<TOKEN>/method).
_BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]{30,}")
_BOT_TOKEN_MASK = "bot<REDACTED>"

# Env var NAME shape that marks its VALUE as secret. Deliberately broad
# (matches *_KEY, *_TOKEN, *_SECRET, *PASSWORD* anywhere in the name) so new
# secret-shaped vars are covered without touching this file.
_SECRET_NAME_RE = re.compile(r".*(_KEY|_TOKEN|_SECRET|PASSWORD).*", re.IGNORECASE)
_VALUE_MASK = "<REDACTED>"

# Below this length a "secret" value is more likely a flag/placeholder
# ("true", "1", "") than real key material — redacting it would butcher
# ordinary log text for no security benefit.
_MIN_SECRET_LEN = 8


def _collect_secret_values(environ: Mapping[str, str]) -> list[str]:
    """Snapshot env-var values whose NAME looks secret-shaped.

    Longest-first so a short secret that happens to be a substring of a
    longer one never leaves a truncated tail of the longer one exposed.
    """
    values = [
        value
        for name, value in environ.items()
        if value and len(value) >= _MIN_SECRET_LEN and _SECRET_NAME_RE.match(name)
    ]
    values.sort(key=len, reverse=True)
    return values


class SecretRedactionFilter(logging.Filter):
    """Masks Telegram bot tokens and secret-env-var values in every record.

    Secret values are read from ``environ`` ONCE at construction time (a
    snapshot, not a live view) — tests pass a fake mapping so this never
    depends on, or is confused by, whatever secrets happen to be set in the
    real process environment.
    """

    def __init__(self, environ: Optional[Mapping[str, str]] = None) -> None:
        super().__init__()
        self._values = _collect_secret_values(os.environ if environ is None else environ)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 — redaction must never break logging
            return True
        redacted = self._redact(msg)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True

    def _redact(self, text: str) -> str:
        text = _BOT_TOKEN_RE.sub(_BOT_TOKEN_MASK, text)
        for value in self._values:
            if value in text:
                text = text.replace(value, _VALUE_MASK)
        return text


def install(logger: Optional[logging.Logger] = None) -> SecretRedactionFilter:
    """Attach a fresh `SecretRedactionFilter` to `logger` (root by default).

    A `Logger.addFilter()` alone only runs for records that ORIGINATE on that
    exact logger — records from a child logger (e.g. `httpx`, `telegram`)
    that merely PROPAGATE up to root's handlers skip the root logger's own
    filter list entirely (see Python logging internals: `Logger.handle()`
    applies `self.filter()` once at the origin, then `callHandlers()` walks
    up invoking each ancestor's HANDLERS directly — the ancestor Logger
    objects are never filtered again). So the filter must also be attached to
    every handler already on `logger` (typically the one `logging.basicConfig`
    installs) — a Handler's `filter()` runs for every record that flows
    through it, regardless of which logger it originated on. That is what
    actually makes httpx/telegram output get redacted here. Call this AFTER
    `logging.basicConfig()` (or whatever attaches the handlers) so
    `logger.handlers` is already populated.

    Returns the filter instance so callers (and tests) can inspect it.
    """
    target = logging.getLogger() if logger is None else logger
    filt = SecretRedactionFilter()
    target.addFilter(filt)
    for handler in target.handlers:
        handler.addFilter(filt)
    return filt


def quiet_http_loggers() -> None:
    """Cap httpx/httpcore loggers at WARNING regardless of the root level.

    Both libraries log the full outbound request line (URL + method) at
    INFO — with LOG_LEVEL=DEBUG or INFO that line includes the Telegram bot
    token embedded in the URL. The redaction filter above masks it wherever
    it ends up, but this stops the noisy/sensitive line from being emitted
    at all in the common case. LOG_HTTP_LEVEL overrides for debugging.
    """
    level_name = os.environ.get("LOG_HTTP_LEVEL", "WARNING").strip().upper()
    level = getattr(logging, level_name, logging.WARNING)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(level)
