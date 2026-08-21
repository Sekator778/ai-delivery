"""Tests for bot/log_redact.py — the root-logger secret redaction filter.

Fixture secrets are assembled at runtime (never a contiguous literal in
source) so this file itself can never be a gitleaks finding — same
self-poisoning guard as test_publish_public.py (E9).
"""

from __future__ import annotations

import io
import logging
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "bot"))

import log_redact  # noqa: E402
from log_redact import SecretRedactionFilter, _collect_secret_values  # noqa: E402


def _fake_bot_token() -> str:
    """A Telegram-bot-token-shaped string, built at runtime."""
    digits = "".join(str(d) for d in range(1, 10)) * 2  # "123456789123456789"
    body = "AA" + "H" + "x" * 32  # 35 chars, matches [A-Za-z0-9_-]{30,}
    return f"bot{digits}:{body}"


def _fake_secret_value(n: int = 24) -> str:
    """A random-looking, sufficiently long fake secret VALUE."""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    # Deterministic but not a static literal: derived at call time from index
    # arithmetic rather than typed out as one contiguous string.
    return "".join(alphabet[(i * 7 + 3) % len(alphabet)] for i in range(n))


def _make_record(message: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args,
        exc_info=None,
    )


class BotTokenRedactionTests(unittest.TestCase):
    def test_token_masked_in_url(self) -> None:
        token = _fake_bot_token()
        # Shape of httpx's actual INFO log line: full request URL with the
        # bot token embedded, arriving via %s formatting args (not a
        # pre-joined literal) — matches how httpx calls logger.info().
        record = _make_record(
            "HTTP Request: POST %s/%s/getUpdates \"HTTP/1.1 200 OK\"",
            "https://api.telegram.org", token,
        )
        filt = SecretRedactionFilter(environ={})
        filt.filter(record)
        rendered = record.getMessage()
        self.assertNotIn(token, rendered)
        self.assertNotIn(token.split(":", 1)[1], rendered)
        self.assertIn("bot<REDACTED>", rendered)


class EnvValueRedactionTests(unittest.TestCase):
    def test_env_value_masked_by_name_pattern(self) -> None:
        value = _fake_secret_value()
        fake_environ = {"DEEPSEEK_API_KEY": value, "PATH": "/usr/bin"}
        record = _make_record("using key=%s for backend call", value)
        filt = SecretRedactionFilter(environ=fake_environ)
        filt.filter(record)
        rendered = record.getMessage()
        self.assertNotIn(value, rendered)
        self.assertIn("<REDACTED>", rendered)

    def test_all_name_suffixes_covered(self) -> None:
        for suffix in ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD"):
            value = _fake_secret_value(20)
            values = _collect_secret_values({f"SOME{suffix}": value})
            self.assertIn(value, values, f"suffix {suffix} not picked up")

    def test_non_secret_names_ignored(self) -> None:
        values = _collect_secret_values({
            "HOME": "/Users/someone",
            "LOG_LEVEL": "DEBUG",
            "BOT_HTTP_PORT": "8766",
        })
        self.assertEqual(values, [])

    def test_short_values_untouched(self) -> None:
        """Values under the length floor are left alone — not real secrets."""
        short_value = "abc123"  # 6 chars, below _MIN_SECRET_LEN (8)
        fake_environ = {"SOME_TOKEN": short_value}
        record = _make_record("token=%s", short_value)
        filt = SecretRedactionFilter(environ=fake_environ)
        filt.filter(record)
        rendered = record.getMessage()
        self.assertEqual(rendered, f"token={short_value}")

    def test_longest_first_no_truncated_remainder(self) -> None:
        """A short secret that is a prefix of a longer one must not leave a
        truncated tail of the longer one exposed in the log."""
        long_value = _fake_secret_value(24)
        short_value = long_value[:10]
        fake_environ = {"A_TOKEN": short_value, "B_TOKEN": long_value}
        record = _make_record("vals=%s,%s", short_value, long_value)
        filt = SecretRedactionFilter(environ=fake_environ)
        filt.filter(record)
        rendered = record.getMessage()
        self.assertNotIn(long_value, rendered)
        self.assertNotIn(short_value, rendered)
        self.assertEqual(rendered, "vals=<REDACTED>,<REDACTED>")

    def test_filter_is_idempotent_when_nothing_to_redact(self) -> None:
        record = _make_record("plain message, nothing secret here")
        filt = SecretRedactionFilter(environ={})
        result = filt.filter(record)
        self.assertTrue(result)  # never drops the record
        self.assertEqual(record.getMessage(), "plain message, nothing secret here")


class InstallHandlerPropagationTests(unittest.TestCase):
    """Regression for a real gap: `Logger.addFilter()` alone only runs for
    records ORIGINATING on that logger — a record from a child logger (like
    `httpx` or `telegram`) that merely propagates up to this logger's
    handlers skips this logger's own filter list entirely. `install()` must
    also attach the filter to the logger's HANDLERS (which do see every
    propagated record) — that is what actually redacts httpx/telegram's own
    log calls in bot.py's real setup. Uses an isolated logger pair, never
    the true root, so it cannot leak state into other test modules."""

    def setUp(self) -> None:
        self.parent = logging.getLogger("test_log_redact_parent")
        self.parent.handlers.clear()
        self.parent.filters.clear()
        self.parent.setLevel(logging.DEBUG)
        self.child = logging.getLogger("test_log_redact_parent.child")
        self.child.setLevel(logging.DEBUG)
        self.buf = io.StringIO()
        self.parent.addHandler(logging.StreamHandler(self.buf))

    def tearDown(self) -> None:
        self.parent.handlers.clear()
        self.parent.filters.clear()

    def test_child_logger_record_redacted_via_installed_handler_filter(self) -> None:
        log_redact.install(self.parent)
        token = _fake_bot_token()
        self.child.info(
            "HTTP Request: POST %s/getUpdates",
            f"https://api.telegram.org/{token}",
        )
        out = self.buf.getvalue()
        self.assertNotIn(token, out)
        self.assertIn("bot<REDACTED>", out)


class MetaEventTruncationTests(unittest.TestCase):
    """Pins the truncation behavior added in bot.py around the
    "[<channel> event] {...}" debug dump — same algorithm as production
    code, verified independently of asyncio/telegram wiring."""

    @staticmethod
    def _truncate(event_str: str, max_len: int) -> str:
        if len(event_str) > max_len:
            return f"{event_str[:max_len]}...<truncated, {len(event_str)} chars total>"
        return event_str

    def test_short_event_untouched(self) -> None:
        short = '{"type": "assistant"}'
        self.assertEqual(self._truncate(short, 2000), short)

    def test_long_event_truncated(self) -> None:
        long_str = "x" * 5000
        result = self._truncate(long_str, 2000)
        self.assertTrue(result.startswith("x" * 2000))
        self.assertIn("truncated, 5000 chars total", result)
        self.assertLess(len(result), len(long_str))

    def test_default_max_matches_bot_module(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "bot"))
        import bot as bot_module  # noqa: E402 — imported lazily, needs full path set

        self.assertEqual(bot_module.META_EVENT_LOG_MAX, 2000)


if __name__ == "__main__":
    unittest.main()
