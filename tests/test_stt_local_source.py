"""Integration tests for the /stt local-path source (_run_stt_local).

Tests two layers:
  1. TestRunSttLocal: _run_stt_local coroutine via bot.py with mocked Telegram
     context and transcribe_voice (no Docker, no network, no Telegram API).
  2. SourceFileIntegrityTests / TranscriptPathDerivationTests: the pure
     stt_utils chain that _run_stt_local relies on, stdlib-only.

bot.py is imported with lightweight stubs for aiohttp and telegram so that
the coroutine tests run without the full project venv.

Coverage:
  - FR-005/AC-2.2  bad extension -> one readable reply, no transcription
  - FR-006/AC-2.3  not found     -> one readable reply, no transcription
  - FR-007/FR-008/FR-009/AC-3.1/AC-3.2  success -> transcript written + reply
  - AC-3.3/FR-010  source file untouched in every branch (ADR-009)
  - NFR-001  RuntimeError / aiohttp.ClientError / asyncio.TimeoutError / OSError
             each produce exactly one readable reply
  - NFR-002  collision -> <stem>-1.txt
  - NFR-003  unicode basename (eszett) survives stem derivation
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "bot"))


# ---------------------------------------------------------------------------
# Stub heavy dependencies so bot.py can be imported without the full venv.
# ---------------------------------------------------------------------------

def _install_stubs() -> None:
    """Install minimal stubs for aiohttp and telegram into sys.modules."""
    if "aiohttp" not in sys.modules:
        aiohttp_mod = types.ModuleType("aiohttp")

        class _ClientError(Exception):
            pass

        class _ClientTimeout:
            def __init__(self, **kw: object) -> None:
                pass

        class _ClientSession:
            def __init__(self, **kw: object) -> None:
                pass

            async def __aenter__(self) -> "_ClientSession":
                return self

            async def __aexit__(self, *a: object) -> None:
                pass

        class _FormData:
            def __init__(self) -> None:
                pass

            def add_field(self, *a: object, **kw: object) -> None:
                pass

        aiohttp_mod.ClientError = _ClientError  # type: ignore[attr-defined]
        aiohttp_mod.ClientTimeout = _ClientTimeout  # type: ignore[attr-defined]
        aiohttp_mod.ClientSession = _ClientSession  # type: ignore[attr-defined]
        aiohttp_mod.FormData = _FormData  # type: ignore[attr-defined]
        web_mod = types.ModuleType("aiohttp.web")
        for _n in ("Application", "AppRunner", "TCPSite", "Response", "Request"):
            setattr(web_mod, _n, type(_n, (), {}))
        aiohttp_mod.web = web_mod  # type: ignore[attr-defined]
        sys.modules["aiohttp"] = aiohttp_mod
        sys.modules["aiohttp.web"] = web_mod

    if "telegram" not in sys.modules:
        tg_mod = types.ModuleType("telegram")
        for _n in (
            "Update", "BotCommand", "ForceReply",
            "InlineKeyboardButton", "InlineKeyboardMarkup",
            "KeyboardButton", "ReplyKeyboardMarkup",
        ):
            setattr(tg_mod, _n, type(_n, (), {}))
        sys.modules["telegram"] = tg_mod

        tgext_mod = types.ModuleType("telegram.ext")
        for _n in (
            "Application", "CallbackQueryHandler",
            "CommandHandler", "MessageHandler",
        ):
            setattr(tgext_mod, _n, type(_n, (), {"__init__": lambda *a, **kw: None}))
        _doc_stub = type("Document", (), {"AUDIO": None})()
        tgext_mod.filters = type(  # type: ignore[attr-defined]
            "filters", (),
            {
                "TEXT": None, "COMMAND": None, "VOICE": None, "AUDIO": None,
                "Document": _doc_stub,
                "CaptionRegex": staticmethod(lambda *a: None),
            },
        )()
        sys.modules["telegram.ext"] = tgext_mod
        tg_mod.ext = tgext_mod  # type: ignore[attr-defined]


_install_stubs()
_ClientError = sys.modules["aiohttp"].ClientError  # type: ignore[attr-defined]

try:
    import bot as _bot_module  # noqa: E402
    _BOT_IMPORTABLE = True
    _SKIP_REASON = ""
except Exception as _import_err:
    _BOT_IMPORTABLE = False
    _SKIP_REASON = str(_import_err)

import stt_utils  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_update(chat_id: int = 12345, message_id: int = 1) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = 42
    update.message.message_id = message_id
    update.message.text = ""
    return update


def _make_context(sent_messages: list | None = None) -> MagicMock:
    ctx = MagicMock()
    captured: list[str] = [] if sent_messages is None else sent_messages
    ctx.bot.send_message = AsyncMock(
        side_effect=lambda chat_id, text, **kw: captured.append(text)
    )
    ctx.bot.send_chat_action = AsyncMock()
    return ctx


def _run(coro: object) -> object:  # type: ignore[type-arg]
    return asyncio.run(coro)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TestRunSttLocal -- coroutine integration tests
# ---------------------------------------------------------------------------

@unittest.skipUnless(_BOT_IMPORTABLE, f"bot.py not importable: {_SKIP_REASON}")
class TestRunSttLocal(unittest.TestCase):
    """_run_stt_local() coroutine -- mocked Telegram + transcribe_voice."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.output_dir = Path(self.tmp) / "transcripts"
        self._patcher = patch.object(_bot_module, "STT_OUTPUT_DIR", self.output_dir)
        self._patcher.start()

    def tearDown(self) -> None:
        self._patcher.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bad_extension_sends_one_reply_naming_path(self) -> None:
        """FR-005/AC-2.2: bad extension -> one reply naming the path."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "notes.txt"
            f.touch()
            raw = str(f)
            msgs: list[str] = []
            ctx = _make_context(msgs)
            with patch.object(
                _bot_module, "transcribe_voice", new_callable=AsyncMock
            ) as mock_tv:
                _run(_bot_module._run_stt_local(_make_update(), ctx, raw, "t01"))
                mock_tv.assert_not_called()
            self.assertEqual(len(msgs), 1)
            self.assertIn(raw, msgs[0])

    def test_not_found_sends_one_reply_naming_path(self) -> None:
        """FR-006/AC-2.3: missing file -> one reply naming the path."""
        with tempfile.TemporaryDirectory() as tmp:
            raw = str(Path(tmp) / "missing.m4a")
            msgs: list[str] = []
            ctx = _make_context(msgs)
            with patch.object(
                _bot_module, "transcribe_voice", new_callable=AsyncMock
            ) as mock_tv:
                _run(_bot_module._run_stt_local(_make_update(), ctx, raw, "t02"))
                mock_tv.assert_not_called()
            self.assertEqual(len(msgs), 1)
            self.assertIn(raw, msgs[0])

    def test_source_file_untouched_after_bad_extension(self) -> None:
        """AC-3.3/FR-010: source preserved on rejection (ADR-009)."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "doc.txt"
            f.write_text("important data", encoding="utf-8")
            mtime_before = f.stat().st_mtime
            ctx = _make_context()
            with patch.object(_bot_module, "transcribe_voice", new_callable=AsyncMock):
                _run(_bot_module._run_stt_local(_make_update(), ctx, str(f), "t03"))
            self.assertTrue(f.exists())
            self.assertEqual(f.read_text(encoding="utf-8"), "important data")
            self.assertEqual(f.stat().st_mtime, mtime_before)

    def test_success_writes_transcript_and_replies_with_path(self) -> None:
        """AC-3.1/AC-3.2: success -> transcript written; reply has abs path + preview."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "interview.m4a"
            f.write_bytes(b"fake audio bytes")
            msgs: list[str] = []
            ctx = _make_context(msgs)
            fake_transcript = "Test transcript content."
            with patch.object(
                _bot_module, "transcribe_voice",
                new_callable=AsyncMock, return_value=fake_transcript,
            ):
                _run(_bot_module._run_stt_local(_make_update(), ctx, str(f), "t04"))
            txt_files = list(self.output_dir.glob("*.txt"))
            self.assertEqual(len(txt_files), 1)
            self.assertEqual(txt_files[0].read_text(encoding="utf-8"), fake_transcript)
            self.assertEqual(len(msgs), 1)
            self.assertIn(str(txt_files[0]), msgs[0])
            self.assertIn(fake_transcript[:50], msgs[0])

    def test_source_file_untouched_after_success(self) -> None:
        """AC-3.3/FR-010: source content unchanged after successful transcription."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "lecture.m4a"
            original_bytes = b"original audio content"
            f.write_bytes(original_bytes)
            mtime_before = f.stat().st_mtime
            ctx = _make_context()
            with patch.object(
                _bot_module, "transcribe_voice",
                new_callable=AsyncMock, return_value="transcript",
            ):
                _run(_bot_module._run_stt_local(_make_update(), ctx, str(f), "t05"))
            self.assertTrue(f.exists())
            self.assertEqual(f.read_bytes(), original_bytes)
            self.assertEqual(f.stat().st_mtime, mtime_before)

    def test_collision_writes_minus_one_suffix(self) -> None:
        """NFR-002: second transcript for the same stem gets -1 suffix."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "talk.m4a"
            f.write_bytes(b"audio")
            with patch.object(
                _bot_module, "transcribe_voice",
                new_callable=AsyncMock, return_value="transcript",
            ):
                _run(_bot_module._run_stt_local(_make_update(), _make_context(), str(f), "t06a"))
                _run(_bot_module._run_stt_local(_make_update(), _make_context(), str(f), "t06b"))
            names = sorted(p.name for p in self.output_dir.glob("*.txt"))
            self.assertIn("talk.txt", names)
            self.assertIn("talk-1.txt", names)

    def test_runtime_error_sends_one_reply(self) -> None:
        """NFR-001: RuntimeError -> one readable reply."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "audio.m4a"
            f.touch()
            msgs: list[str] = []
            ctx = _make_context(msgs)
            with patch.object(
                _bot_module, "transcribe_voice",
                new_callable=AsyncMock,
                side_effect=RuntimeError("STT failed (500): model error"),
            ):
                _run(_bot_module._run_stt_local(_make_update(), ctx, str(f), "t07"))
            self.assertEqual(len(msgs), 1)
            self.assertIn("Ошибка транскрибации", msgs[0])

    def test_client_error_sends_one_reply(self) -> None:
        """NFR-001: aiohttp.ClientError -> one reply about STT being unreachable."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "audio.m4a"
            f.touch()
            msgs: list[str] = []
            ctx = _make_context(msgs)
            with patch.object(
                _bot_module, "transcribe_voice",
                new_callable=AsyncMock,
                side_effect=_ClientError("connection refused"),
            ):
                _run(_bot_module._run_stt_local(_make_update(), ctx, str(f), "t08"))
            self.assertEqual(len(msgs), 1)
            self.assertIn("STT не отвечает", msgs[0])

    def test_timeout_error_sends_one_reply(self) -> None:
        """NFR-001: asyncio.TimeoutError -> one readable reply."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "audio.m4a"
            f.touch()
            msgs: list[str] = []
            ctx = _make_context(msgs)
            with patch.object(
                _bot_module, "transcribe_voice",
                new_callable=AsyncMock,
                side_effect=asyncio.TimeoutError(),
            ):
                _run(_bot_module._run_stt_local(_make_update(), ctx, str(f), "t09"))
            self.assertEqual(len(msgs), 1)
            self.assertIn("STT не отвечает", msgs[0])

    def test_oserror_on_save_sends_one_reply(self) -> None:
        """NFR-001: OSError during transcript save -> one readable reply."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "audio.m4a"
            f.touch()
            msgs: list[str] = []
            ctx = _make_context(msgs)
            with patch.object(
                _bot_module, "transcribe_voice",
                new_callable=AsyncMock, return_value="text",
            ):
                with patch("stt_utils.derive_transcript_path", side_effect=OSError("disk full")):
                    _run(_bot_module._run_stt_local(_make_update(), ctx, str(f), "t10"))
            self.assertEqual(len(msgs), 1)
            self.assertIn("сохранить файл", msgs[0])

    def test_oserror_from_transcribe_voice_sends_one_reply(self) -> None:
        """NFR-001/C-1: OSError from transcribe_voice (TOCTOU race) -> one readable reply
        naming the path.  Before the C-1 fix this exception propagated uncaught and
        produced no reply, violating NFR-001."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "audio.m4a"
            f.touch()
            msgs: list[str] = []
            ctx = _make_context(msgs)
            with patch.object(
                _bot_module, "transcribe_voice",
                new_callable=AsyncMock,
                side_effect=FileNotFoundError("file gone between check and open"),
            ):
                _run(_bot_module._run_stt_local(_make_update(), ctx, str(f), "c1"))
            self.assertEqual(len(msgs), 1, "expected exactly one reply on OSError from transcribe_voice")
            self.assertIn(str(f), msgs[0], "reply must name the offending path")

    def test_oversized_file_rejected_before_transcription(self) -> None:
        """W-3/NFR-001: file > STT_URL_MAX_MB limit → one reply, transcribe_voice not called.

        Sets STT_URL_MAX_MB=0 so that any non-empty file (1 byte) exceeds the cap.
        Before the W-3 fix, file_size was logged but never checked, so transcription
        proceeded even for arbitrarily large files.
        """
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "huge.m4a"
            f.write_bytes(b"x")  # 1 byte — will exceed 0 MB cap
            msgs: list[str] = []
            ctx = _make_context(msgs)
            with patch.object(_bot_module, "STT_URL_MAX_MB", 0), \
                 patch.object(
                     _bot_module, "transcribe_voice", new_callable=AsyncMock
                 ) as mock_tv:
                _run(_bot_module._run_stt_local(_make_update(), ctx, str(f), "w3a"))
                mock_tv.assert_not_called()
            self.assertEqual(len(msgs), 1, "expected exactly one rejection reply")
            self.assertIn("0", msgs[0], "reply must mention the size limit (0 MB)")

    def test_file_at_size_limit_is_not_rejected(self) -> None:
        """Regression/W-3: file exactly at the byte-cap boundary must proceed to transcription.

        Boundary check: file_size > limit (strict), so a file exactly equal to the
        limit is allowed through.  STT_URL_MAX_MB=0 → 0 MB → 0 bytes; an empty
        file (0 bytes) must not be rejected.
        """
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "zero.m4a"
            f.write_bytes(b"")  # 0 bytes — exactly at the 0 MB limit, not over
            msgs: list[str] = []
            ctx = _make_context(msgs)
            with patch.object(_bot_module, "STT_URL_MAX_MB", 0), \
                 patch.object(
                     _bot_module, "transcribe_voice",
                     new_callable=AsyncMock, return_value="transcript",
                 ) as mock_tv:
                _run(_bot_module._run_stt_local(_make_update(), ctx, str(f), "w3b"))
                mock_tv.assert_called_once()

    def test_source_file_untouched_after_runtime_error(self) -> None:
        """AC-3.3/FR-010: no finally block -- source never touched on error."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "audio.m4a"
            original = b"my precious audio"
            f.write_bytes(original)
            ctx = _make_context()
            with patch.object(
                _bot_module, "transcribe_voice",
                new_callable=AsyncMock,
                side_effect=RuntimeError("server down"),
            ):
                _run(_bot_module._run_stt_local(_make_update(), ctx, str(f), "t11"))
            self.assertTrue(f.exists())
            self.assertEqual(f.read_bytes(), original)

    def test_stem_derived_from_unicode_basename(self) -> None:
        """FR-008/NFR-003: stem from unicode basename produces correct transcript name."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "Wattstraße 8.m4a"
            f.write_bytes(b"audio")
            ctx = _make_context()
            with patch.object(
                _bot_module, "transcribe_voice",
                new_callable=AsyncMock, return_value="text",
            ):
                _run(_bot_module._run_stt_local(_make_update(), ctx, str(f), "t12"))
            names = [p.name for p in self.output_dir.glob("*.txt")]
            self.assertEqual(len(names), 1)
            self.assertEqual(names[0], "Wattstraße 8.txt")


# ---------------------------------------------------------------------------
# SourceFileIntegrityTests -- pure stt_utils: no source modification
# ---------------------------------------------------------------------------

class SourceFileIntegrityTests(unittest.TestCase):
    """Source audio file must be unchanged after any check_local_audio_path call."""

    def test_bad_extension_does_not_modify_source(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as fh:
            fh.write(b"original content - must not be touched")
            path = fh.name
        try:
            before_stat = os.stat(path)
            result = stt_utils.check_local_audio_path(path)
            self.assertEqual(result.verdict, "bad_extension")
            after_stat = os.stat(path)
            self.assertEqual(before_stat.st_size, after_stat.st_size)
            self.assertEqual(before_stat.st_mtime, after_stat.st_mtime)
        finally:
            os.unlink(path)

    def test_not_found_does_not_create_file(self) -> None:
        path = "/tmp/__stt_integration_nonexistent_7h3q.m4a"
        self.assertFalse(os.path.exists(path))
        result = stt_utils.check_local_audio_path(path)
        self.assertEqual(result.verdict, "not_found")
        self.assertFalse(os.path.exists(path))

    def test_ok_verdict_does_not_modify_source(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as fh:
            fh.write(b"fake audio bytes - must survive validation")
            path = fh.name
        try:
            before_stat = os.stat(path)
            result = stt_utils.check_local_audio_path(path)
            self.assertEqual(result.verdict, "ok")
            after_stat = os.stat(path)
            self.assertEqual(before_stat.st_size, after_stat.st_size)
            self.assertEqual(before_stat.st_mtime, after_stat.st_mtime)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# TranscriptPathDerivationTests -- stem from basename + collision rule
# ---------------------------------------------------------------------------

class TranscriptPathDerivationTests(unittest.TestCase):
    """Transcript naming: stem from source basename + collision-safe write."""

    def test_stem_from_source_basename_no_separators(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as fh:
            fh.write(b"audio")
            path = fh.name
        try:
            result = stt_utils.check_local_audio_path(path)
            self.assertEqual(result.verdict, "ok")
            stem = stt_utils.sanitize_filename(result.path.name)
            self.assertGreater(len(stem), 0)
            self.assertNotIn("/", stem)
            self.assertNotIn("..", stem)
        finally:
            os.unlink(path)

    def test_unicode_stem_eszett_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "Wattstraße 8.m4a")
            with open(src, "wb") as fh:
                fh.write(b"audio")
            result = stt_utils.check_local_audio_path(src)
            self.assertEqual(result.verdict, "ok")
            stem = stt_utils.sanitize_filename(result.path.name)
            self.assertIn("Wattstra", stem)

    def test_first_transcript_no_collision_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as out_dir:
            out = Path(out_dir)
            p = stt_utils.derive_transcript_path(out, "interview")
            self.assertEqual(p.name, "interview.txt")
            self.assertTrue(p.exists())

    def test_second_transcript_gets_minus_one_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as out_dir:
            out = Path(out_dir)
            p1 = stt_utils.derive_transcript_path(out, "recording")
            p2 = stt_utils.derive_transcript_path(out, "recording")
            self.assertEqual(p1.name, "recording.txt")
            self.assertEqual(p2.name, "recording-1.txt")
            self.assertNotEqual(p1, p2)

    def test_transcript_write_does_not_touch_source(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "recording.m4a")
            with open(src, "wb") as fh:
                fh.write(b"source audio content")
            out = Path(d) / "transcripts"
            result = stt_utils.check_local_audio_path(src)
            self.assertEqual(result.verdict, "ok")
            stem = stt_utils.sanitize_filename(result.path.name)
            transcript_path = stt_utils.derive_transcript_path(out, stem)
            transcript_path.write_text("Hello, this is the transcript.", encoding="utf-8")
            with open(src, "rb") as fh:
                self.assertEqual(fh.read(), b"source audio content")


# ---------------------------------------------------------------------------
# ErrorDisplayTests -- display field carries exact verbatim path
# ---------------------------------------------------------------------------

class ErrorDisplayTests(unittest.TestCase):
    """display field in LocalPathCheck carries the verbatim input path."""

    def test_bad_extension_display_is_verbatim_input(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as fh:
            path = fh.name
        try:
            result = stt_utils.check_local_audio_path(path)
            self.assertEqual(result.verdict, "bad_extension")
            self.assertEqual(result.display, path)
        finally:
            os.unlink(path)

    def test_not_found_display_is_verbatim_input(self) -> None:
        raw = "/tmp/__nonexistent_stt_test_abc123.m4a"
        result = stt_utils.check_local_audio_path(raw)
        self.assertEqual(result.verdict, "not_found")
        self.assertEqual(result.display, raw)

    def test_ok_display_is_verbatim_input(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as fh:
            path = fh.name
        try:
            result = stt_utils.check_local_audio_path(path)
            self.assertEqual(result.verdict, "ok")
            self.assertEqual(result.display, path)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# UnknownCommandProbeTests -- GAP 1: all four branches of unknown_command_probe
# ---------------------------------------------------------------------------

@unittest.skipUnless(_BOT_IMPORTABLE, f"bot.py not importable: {_SKIP_REASON}")
class UnknownCommandProbeTests(unittest.TestCase):
    """unknown_command_probe handler — four dispatch branches (GAP 1).

    FR-001/AC-1.1: plain-text local-path candidate with mode ON dispatches to
    _run_stt_local.  FR-011/AC-4.1: unauthorized sender → silent reject, no
    reply, no transcription.  Mode OFF or non-path-candidate text → bare
    return with no side effects.
    """

    def _make_probe_update(self, text: str = "") -> MagicMock:
        update = _make_update()
        update.message.text = text
        return update

    def test_unauthorized_user_gets_no_reply_and_no_transcription(self) -> None:
        """FR-011/AC-4.1: user not in USER_REGISTRY → silent reject, nothing sent."""
        update = self._make_probe_update("/Users/owner/rec.m4a")
        msgs: list[str] = []
        ctx = _make_context(msgs)
        with patch.object(_bot_module, "USER_REGISTRY", set()), \
             patch.object(_bot_module, "_run_stt_local", new_callable=AsyncMock) as mock_local:
            _run(_bot_module.unknown_command_probe(update, ctx))
            mock_local.assert_not_called()
        self.assertEqual(len(msgs), 0)

    def test_mode_off_returns_silently_without_dispatch(self) -> None:
        """Mode OFF: bare return even when text is path-shaped — no _run_stt_local."""
        update = self._make_probe_update("/Users/owner/rec.m4a")
        msgs: list[str] = []
        ctx = _make_context(msgs)
        with patch.object(_bot_module, "USER_REGISTRY", {42}), \
             patch.object(_bot_module, "_stt_mode_on", return_value=False), \
             patch.object(_bot_module, "_run_stt_local", new_callable=AsyncMock) as mock_local:
            _run(_bot_module.unknown_command_probe(update, ctx))
            mock_local.assert_not_called()
        self.assertEqual(len(msgs), 0)

    def test_mode_on_non_path_candidate_returns_silently(self) -> None:
        """Mode ON + non-path-candidate text: bare return, no dispatch."""
        # "hello world" does not start with "/" or "~/" → is_local_path_candidate False.
        update = self._make_probe_update("hello world")
        msgs: list[str] = []
        ctx = _make_context(msgs)
        with patch.object(_bot_module, "USER_REGISTRY", {42}), \
             patch.object(_bot_module, "_stt_mode_on", return_value=True), \
             patch.object(_bot_module, "_run_stt_local", new_callable=AsyncMock) as mock_local:
            _run(_bot_module.unknown_command_probe(update, ctx))
            mock_local.assert_not_called()
        self.assertEqual(len(msgs), 0)

    def test_mode_on_path_text_dispatches_to_run_stt_local_with_correct_arg(self) -> None:
        """FR-001/AC-1.1: mode ON + path text → _run_stt_local called with verbatim path."""
        path_text = "/Users/owner/recording.m4a"
        update = self._make_probe_update(path_text)
        msgs: list[str] = []
        ctx = _make_context(msgs)
        with patch.object(_bot_module, "USER_REGISTRY", {42}), \
             patch.object(_bot_module, "_stt_mode_on", return_value=True), \
             patch.object(_bot_module, "_run_stt_local", new_callable=AsyncMock) as mock_local:
            _run(_bot_module.unknown_command_probe(update, ctx))
            mock_local.assert_called_once()
            # Third positional arg (index 2) is raw_path forwarded to _run_stt_local.
            self.assertEqual(mock_local.call_args[0][2], path_text)

    def test_unknown_command_probe_handler_registered_with_block_false(self) -> None:
        """W-2: MessageHandler(unknown_command_probe) must have block=False.

        Without block=False the PTB dispatcher cannot process any update while
        a slow transcription coroutine is running, freezing the bot for all users.
        The other two heavy handlers (handle_text, handle_voice) are both registered
        block=False; this handler must match that pattern.

        This test reads the bot.py source to assert the registration keyword is
        present.  It fails before the W-2 fix and passes after.
        """
        import pathlib, re
        src = pathlib.Path(_bot_module.__file__).read_text(encoding="utf-8")
        # The MessageHandler call for unknown_command_probe must contain block=False
        # as a keyword argument.  After the fix the single logical line looks like:
        #   MessageHandler(filters.TEXT & filters.COMMAND, unknown_command_probe, block=False)
        found = bool(re.search(
            r"MessageHandler\s*\([^)]*unknown_command_probe[^)]*block\s*=\s*False[^)]*\)",
            src,
            re.DOTALL,
        ))
        self.assertTrue(
            found,
            "MessageHandler(unknown_command_probe) must include block=False (W-2): "
            "a blocking handler freezes update processing during long transcriptions.",
        )


# ---------------------------------------------------------------------------
# SttCommandModePreservationTests -- GAP 2: mode unchanged when path arg given
# ---------------------------------------------------------------------------

@unittest.skipUnless(_BOT_IMPORTABLE, f"bot.py not importable: {_SKIP_REASON}")
class SttCommandModePreservationTests(unittest.TestCase):
    """AC-1.3/FR-003/AC-5.2: /stt <path> dispatches without touching per-user mode.

    Tests the actual stt_command handler (not just classify_stt_command) to
    verify _set_stt_mode is never called when a path-shaped argument is detected,
    and conversely that _set_stt_mode IS called for non-path arguments.
    """

    def _make_stt_update(self, text: str, cmd_len: int = 4) -> MagicMock:
        """Build an Update stub for stt_command with no reply-to audio."""
        update = _make_update()
        # Explicit None prevents _extract_audio_source from seeing a truthy MagicMock.
        update.message.reply_to_message = None
        update.message.text = text
        entity = MagicMock()
        entity.offset = 0
        entity.length = cmd_len
        update.message.entities = [entity]
        return update

    def test_path_arg_does_not_call_set_stt_mode(self) -> None:
        """AC-1.3/FR-003: path-shaped arg → _set_stt_mode never called; _run_stt_local is."""
        update = self._make_stt_update("/stt /recordings/session.m4a")
        ctx = _make_context()
        with patch.object(_bot_module, "USER_REGISTRY", {42}), \
             patch.object(_bot_module, "_set_stt_mode") as mock_set, \
             patch.object(_bot_module, "_run_stt_local", new_callable=AsyncMock) as mock_local:
            _run(_bot_module.stt_command(update, ctx))
            mock_set.assert_not_called()
            mock_local.assert_called_once()
            # Confirm the exact path (with spaces preserved) is forwarded.
            self.assertEqual(mock_local.call_args[0][2], "/recordings/session.m4a")

    def test_non_path_arg_does_call_set_stt_mode(self) -> None:
        """AC-1.3/FR-003: non-path arg → _set_stt_mode IS called; _run_stt_local is not."""
        update = self._make_stt_update("/stt some unrelated text")
        msgs: list[str] = []
        ctx = _make_context(msgs)
        with patch.object(_bot_module, "USER_REGISTRY", {42}), \
             patch.object(_bot_module, "_stt_mode_on", return_value=False), \
             patch.object(_bot_module, "_set_stt_mode") as mock_set, \
             patch.object(_bot_module, "_run_stt_local", new_callable=AsyncMock) as mock_local:
            _run(_bot_module.stt_command(update, ctx))
            mock_set.assert_called_once()
            mock_local.assert_not_called()


# ---------------------------------------------------------------------------
# TildeExpansionEndToEndTests -- GAP 3: ~/... path through _run_stt_local
# ---------------------------------------------------------------------------

@unittest.skipUnless(_BOT_IMPORTABLE, f"bot.py not importable: {_SKIP_REASON}")
class TildeExpansionEndToEndTests(unittest.TestCase):
    """AC-2.1/FR-004: tilde expansion end-to-end through _run_stt_local (GAP 3).

    stt_utils.check_local_audio_path is unit-tested with expanduser in
    test_stt_utils.py.  These tests cover the full _run_stt_local coroutine:
    a raw ~/... path reaching the handler is expanded before the existence
    check, allowing the file to be found and transcription to proceed (or the
    not-found error to name the expanded path).
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.output_dir = Path(self.tmp) / "transcripts"
        self._patcher = patch.object(_bot_module, "STT_OUTPUT_DIR", self.output_dir)
        self._patcher.start()

    def tearDown(self) -> None:
        self._patcher.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tilde_path_expands_to_real_file_and_transcription_proceeds(self) -> None:
        """AC-2.1/FR-004: ~/interview.m4a expanded to real path; transcription runs."""
        with tempfile.TemporaryDirectory() as audio_dir:
            real_file = Path(audio_dir) / "interview.m4a"
            real_file.write_bytes(b"fake audio bytes for tilde test")
            msgs: list[str] = []
            ctx = _make_context(msgs)
            fake_text = "Tilde expansion transcript content here."
            # Patch expanduser so that any ~/... input resolves to real_file.
            with patch("os.path.expanduser", return_value=str(real_file)), \
                 patch.object(
                     _bot_module, "transcribe_voice",
                     new_callable=AsyncMock, return_value=fake_text,
                 ):
                _run(_bot_module._run_stt_local(
                    _make_update(), ctx, "~/interview.m4a", "te01"
                ))
            # Exactly one reply — the success reply, not an error message.
            self.assertEqual(len(msgs), 1)
            self.assertNotIn("не найден", msgs[0])
            self.assertIn(fake_text[:50], msgs[0])
            # Transcript file was written to the output directory.
            txt_files = list(self.output_dir.glob("*.txt"))
            self.assertEqual(len(txt_files), 1)

    def test_tilde_path_expanded_but_file_missing_sends_not_found_error(self) -> None:
        """AC-2.1/FR-004/FR-006: ~/missing.m4a expanded; no file → named-error reply."""
        msgs: list[str] = []
        ctx = _make_context(msgs)
        with patch("os.path.expanduser", return_value="/nonexistent/path/missing.m4a"), \
             patch.object(_bot_module, "transcribe_voice", new_callable=AsyncMock) as mock_tv:
            _run(_bot_module._run_stt_local(
                _make_update(), ctx, "~/missing.m4a", "te02"
            ))
            mock_tv.assert_not_called()
        self.assertEqual(len(msgs), 1)
        self.assertIn("не найден", msgs[0])


# ---------------------------------------------------------------------------
# HandleTextLocalPathRoutingTests -- W-4: handle_text routes tilde paths to
# _run_stt_local when STT mode is ON (AC-1.1/FR-001)
# ---------------------------------------------------------------------------

@unittest.skipUnless(_BOT_IMPORTABLE, f"bot.py not importable: {_SKIP_REASON}")
class HandleTextLocalPathRoutingTests(unittest.TestCase):
    """W-4: handle_text local-path branch (bot.py lines 2038-2045, FR-001/AC-1.1).

    Tests the actual handle_text handler — not just _run_stt_local — to verify
    that the routing guard `_stt_mode_on(user_id) and is_local_path_candidate(text)`
    is wired correctly.  Removing or inverting that condition would cause the first
    test in this class to fail with _run_stt_local.assert_called_once() raising.
    """

    def _make_text_update(self, text: str) -> MagicMock:
        update = _make_update()
        update.message.text = text
        # None prevents _maybe_handle_clarify_reply from short-circuiting.
        update.message.reply_to_message = None
        return update

    def test_tilde_path_while_mode_on_dispatches_to_run_stt_local(self) -> None:
        """FR-001/AC-1.1: ~/... plain text with mode ON → _run_stt_local called
        with the verbatim (stripped) path; handle_text returns without Q&A.

        Before W-4 this routing was only tested via _run_stt_local directly.
        Removing the seven-line guard in handle_text would leave all prior tests
        green while silently breaking the tilde-path entry point."""
        path_text = "~/recordings/meeting.m4a"
        update = self._make_text_update(path_text)
        ctx = _make_context()
        with patch.object(_bot_module, "USER_REGISTRY", {42: {"name": "owner", "meta_dir": "/tmp"}}), \
             patch.object(_bot_module, "_stt_mode_on", return_value=True), \
             patch.object(_bot_module, "_run_stt_local", new_callable=AsyncMock) as mock_local:
            _run(_bot_module.handle_text(update, ctx))
            mock_local.assert_called_once()
            # Third positional arg is raw_path; handle_text passes text_content.strip()
            self.assertEqual(mock_local.call_args[0][2], path_text.strip())

    def test_absolute_path_while_mode_on_dispatches_to_run_stt_local(self) -> None:
        """Regression/FR-001: /abs/path text with mode ON → _run_stt_local called."""
        path_text = "/data/audio/lecture.mp3"
        update = self._make_text_update(path_text)
        ctx = _make_context()
        with patch.object(_bot_module, "USER_REGISTRY", {42: {"name": "owner", "meta_dir": "/tmp"}}), \
             patch.object(_bot_module, "_stt_mode_on", return_value=True), \
             patch.object(_bot_module, "_run_stt_local", new_callable=AsyncMock) as mock_local:
            _run(_bot_module.handle_text(update, ctx))
            mock_local.assert_called_once()
            self.assertEqual(mock_local.call_args[0][2], path_text.strip())

    def test_tilde_path_while_mode_off_does_not_dispatch_to_run_stt_local(self) -> None:
        """Regression/FR-001: mode OFF → _run_stt_local NOT called; falls to Q&A."""
        path_text = "~/recordings/meeting.m4a"
        update = self._make_text_update(path_text)
        ctx = _make_context()
        with patch.object(_bot_module, "USER_REGISTRY", {42: {"name": "owner", "meta_dir": "/tmp"}}), \
             patch.object(_bot_module, "_stt_mode_on", return_value=False), \
             patch.object(_bot_module, "_run_stt_local", new_callable=AsyncMock) as mock_local, \
             patch.object(_bot_module, "_ack_queued_behind_meta", new_callable=AsyncMock), \
             patch.object(_bot_module, "run_meta_claude", new_callable=AsyncMock), \
             patch.object(_bot_module, "load_state", return_value={}), \
             patch.object(_bot_module, "save_state"):
            _run(_bot_module.handle_text(update, ctx))
            mock_local.assert_not_called()


if __name__ == "__main__":
    unittest.main()
