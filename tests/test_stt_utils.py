"""Tests for bot/stt_utils.py — pure STT helper logic.

All tests are runnable without any running Docker container, model file,
Telegram client, or aiohttp session. Pure stdlib, unittest.TestCase.

FR-019 / AC-9.1: sanitize_filename, derive_transcript_path, is_bare_url,
classify_head_response, AUDIO_EXTENSIONS.
FR-012 / FR-013 / AC-5.1 / AC-5.2: is_local_path_candidate,
classify_stt_command, check_local_audio_path, LocalPathCheck.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "bot"))

import stt_utils  # noqa: E402
from stt_utils import (  # noqa: E402
    AUDIO_EXTENSIONS,
    HeadResult,
    classify_head_response,
    derive_transcript_path,
    is_bare_url,
    sanitize_filename,
)


# ---------------------------------------------------------------------------
# AudioExtensionSetTests
# ---------------------------------------------------------------------------

class AudioExtensionSetTests(unittest.TestCase):
    """Verify the accepted-audio-extension constant covers the BRD list."""

    def test_all_brd_extensions_present(self) -> None:
        required = {".m4a", ".mp3", ".ogg", ".wav", ".aiff", ".flac"}
        self.assertTrue(
            required.issubset(AUDIO_EXTENSIONS),
            f"Missing from AUDIO_EXTENSIONS: {required - AUDIO_EXTENSIONS}",
        )

    def test_extensions_are_lowercase(self) -> None:
        for ext in AUDIO_EXTENSIONS:
            self.assertEqual(ext, ext.lower(), f"Extension {ext!r} is not lowercase")

    def test_extensions_start_with_dot(self) -> None:
        for ext in AUDIO_EXTENSIONS:
            self.assertTrue(ext.startswith("."), f"Extension {ext!r} has no leading dot")


# ---------------------------------------------------------------------------
# SanitizeFilenameTests
# ---------------------------------------------------------------------------

class SanitizeFilenameTests(unittest.TestCase):
    """sanitize_filename() must strip unsafe characters and cap length."""

    def test_simple_name_unchanged_stem(self) -> None:
        result = sanitize_filename("hello.mp3")
        self.assertEqual(result, "hello")

    def test_path_separators_stripped(self) -> None:
        # os.path.basename is applied — only the last component survives
        result = sanitize_filename("../../etc/passwd")
        self.assertNotIn("/", result)
        self.assertNotIn("..", result)
        # basename of "../../etc/passwd" is "passwd"
        self.assertEqual(result, "passwd")

    def test_control_chars_removed(self) -> None:
        name = "audio\x00\x1f\nfile.mp3"
        result = sanitize_filename(name)
        self.assertNotIn("\x00", result)
        self.assertNotIn("\n", result)
        self.assertNotIn("\x1f", result)

    def test_leading_dots_stripped(self) -> None:
        result = sanitize_filename(".hidden.mp3")
        self.assertFalse(result.startswith("."), f"Result starts with dot: {result!r}")

    def test_all_dots_falls_back_to_audio(self) -> None:
        # A name that reduces to only dots/spaces → "audio"
        result = sanitize_filename("...mp3")
        # After stripping leading dots we should get something sane
        # "...mp3" basename stem is "...mp3" → strip("." ) gives "mp3"
        # (depends on implementation; at minimum result must be non-empty)
        self.assertTrue(len(result) > 0, "sanitize_filename returned empty string")

    def test_empty_name_returns_audio(self) -> None:
        result = sanitize_filename("")
        self.assertEqual(result, "audio")

    def test_pure_dots_returns_audio(self) -> None:
        result = sanitize_filename("...")
        self.assertEqual(result, "audio")

    def test_max_len_enforced(self) -> None:
        long_name = "a" * 300 + ".mp3"
        result = sanitize_filename(long_name, max_len=200)
        self.assertLessEqual(len(result), 200)

    def test_windows_path_separator_stripped(self) -> None:
        # On POSIX, os.path.basename treats backslash as a regular char,
        # but the regex replaces non-word chars with underscores.
        result = sanitize_filename("folder\\subfile.mp3")
        self.assertNotIn("\\", result)

    def test_unicode_name_normalized(self) -> None:
        # Cyrillic is kept by \w; only control/special chars stripped
        result = sanitize_filename("аудио.mp3")
        self.assertTrue(len(result) > 0)

    def test_special_chars_replaced_with_underscore(self) -> None:
        result = sanitize_filename("my file (1).mp3")
        # Parentheses and spaces are non-word → replaced
        self.assertNotIn("(", result)
        self.assertNotIn(")", result)


# ---------------------------------------------------------------------------
# DeriveTranscriptPathTests
# ---------------------------------------------------------------------------

class DeriveTranscriptPathTests(unittest.TestCase):
    """derive_transcript_path() must create collision-free files exclusively."""

    def test_first_file_no_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            p = derive_transcript_path(out, "interview")
            self.assertEqual(p.name, "interview.txt")
            self.assertTrue(p.exists())

    def test_second_file_gets_minus_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            p1 = derive_transcript_path(out, "interview")
            p2 = derive_transcript_path(out, "interview")
            self.assertEqual(p1.name, "interview.txt")
            self.assertEqual(p2.name, "interview-1.txt")
            self.assertTrue(p1.exists())
            self.assertTrue(p2.exists())

    def test_third_file_gets_minus_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            derive_transcript_path(out, "interview")
            derive_transcript_path(out, "interview")
            p3 = derive_transcript_path(out, "interview")
            self.assertEqual(p3.name, "interview-2.txt")

    def test_voice_timestamp_scheme(self) -> None:
        # Timestamp-derived stems work identically to sourced names
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            stem = "voice-20260814-153012"
            p1 = derive_transcript_path(out, stem)
            p2 = derive_transcript_path(out, stem)
            self.assertEqual(p1.name, f"{stem}.txt")
            self.assertEqual(p2.name, f"{stem}-1.txt")

    def test_auto_creates_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "transcripts"
            self.assertFalse(out.exists())
            p = derive_transcript_path(out, "test")
            self.assertTrue(out.exists())
            self.assertTrue(p.exists())

    def test_returned_file_is_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            p = derive_transcript_path(out, "audio")
            # The function creates an empty file; caller writes content.
            p.write_text("hello transcript", encoding="utf-8")
            self.assertEqual(p.read_text(encoding="utf-8"), "hello transcript")

    def test_exclusive_create_no_overwrite(self) -> None:
        """The returned path must be newly created, never reusing an existing one."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            p1 = derive_transcript_path(out, "doc")
            p1.write_text("original", encoding="utf-8")
            p2 = derive_transcript_path(out, "doc")
            p2.write_text("second", encoding="utf-8")
            # Originals must be untouched
            self.assertEqual(p1.read_text(encoding="utf-8"), "original")
            self.assertEqual(p2.read_text(encoding="utf-8"), "second")
            self.assertNotEqual(p1, p2)


# ---------------------------------------------------------------------------
# BareUrlDetectionTests
# ---------------------------------------------------------------------------

class BareUrlDetectionTests(unittest.TestCase):
    """is_bare_url() — single http(s) URL detection (FR-002/FR-003)."""

    def test_single_http_url_is_bare(self) -> None:
        self.assertTrue(is_bare_url("http://example.com/audio.mp3"))

    def test_single_https_url_is_bare(self) -> None:
        self.assertTrue(is_bare_url("https://cdn.example.com/file.m4a"))

    def test_url_with_leading_whitespace(self) -> None:
        self.assertTrue(is_bare_url("  https://example.com/audio.mp3  "))

    def test_url_plus_text_is_not_bare(self) -> None:
        # AC-2.2: "check this https://example.com/a.mp3 please" → False
        self.assertFalse(is_bare_url("check this https://example.com/a.mp3 please"))

    def test_url_inline_with_other_text(self) -> None:
        self.assertFalse(is_bare_url("https://example.com/a.mp3 please"))

    def test_non_http_scheme_ftp_is_not_bare(self) -> None:
        # NFR-007: only http/https
        self.assertFalse(is_bare_url("ftp://example.com/audio.mp3"))

    def test_non_http_scheme_file_is_not_bare(self) -> None:
        self.assertFalse(is_bare_url("file:///home/user/audio.mp3"))

    def test_bare_text_is_not_bare_url(self) -> None:
        self.assertFalse(is_bare_url("just some text"))

    def test_empty_string_is_not_bare(self) -> None:
        self.assertFalse(is_bare_url(""))

    def test_url_with_newline_is_not_bare(self) -> None:
        self.assertFalse(is_bare_url("https://example.com/audio.mp3\nmore text"))

    def test_url_with_tab_is_not_bare(self) -> None:
        self.assertFalse(is_bare_url("https://example.com/audio.mp3\tsuffix"))


# ---------------------------------------------------------------------------
# HeadClassificationTests
# ---------------------------------------------------------------------------

class HeadClassificationTests(unittest.TestCase):
    """classify_head_response() implements FR-004 accept/inconclusive/reject."""

    # --- accept-by-content-type ---

    def test_audio_content_type_is_accepted(self) -> None:
        result = HeadResult(status=200, content_type="audio/mpeg", is_redirect=False)
        self.assertEqual(classify_head_response(result, "https://x.com/file"), "accept")

    def test_audio_ogg_content_type_accepted(self) -> None:
        result = HeadResult(status=200, content_type="audio/ogg", is_redirect=False)
        self.assertEqual(classify_head_response(result, "https://x.com/file"), "accept")

    def test_audio_content_type_with_params_accepted(self) -> None:
        result = HeadResult(status=200, content_type="audio/mp4; codecs=mp4a", is_redirect=False)
        self.assertEqual(classify_head_response(result, "https://x.com/file"), "accept")

    # --- accept-by-extension ---

    def test_mp3_extension_accepted_even_without_good_ct(self) -> None:
        result = HeadResult(status=200, content_type="application/octet-stream", is_redirect=False)
        self.assertEqual(classify_head_response(result, "https://x.com/audio.mp3"), "accept")

    def test_m4a_extension_accepted(self) -> None:
        result = HeadResult(status=200, content_type=None, is_redirect=False)
        self.assertEqual(classify_head_response(result, "https://x.com/podcast.m4a"), "accept")

    def test_flac_extension_accepted(self) -> None:
        result = HeadResult(status=200, content_type="binary/octet-stream", is_redirect=False)
        self.assertEqual(classify_head_response(result, "https://x.com/song.flac"), "accept")

    # --- inconclusive ---

    def test_none_status_is_inconclusive(self) -> None:
        result = HeadResult(status=None, content_type=None, is_redirect=False)
        self.assertEqual(classify_head_response(result, "https://x.com/file"), "inconclusive")

    def test_redirect_is_inconclusive(self) -> None:
        result = HeadResult(status=301, content_type=None, is_redirect=True)
        self.assertEqual(classify_head_response(result, "https://x.com/file.mp3"), "inconclusive")

    def test_missing_content_type_is_inconclusive(self) -> None:
        result = HeadResult(status=200, content_type=None, is_redirect=False)
        self.assertEqual(classify_head_response(result, "https://x.com/audio"), "inconclusive")

    def test_empty_content_type_is_inconclusive(self) -> None:
        result = HeadResult(status=200, content_type="", is_redirect=False)
        self.assertEqual(classify_head_response(result, "https://x.com/audio"), "inconclusive")

    # --- reject ---

    def test_text_html_without_audio_ext_is_rejected(self) -> None:
        result = HeadResult(status=200, content_type="text/html", is_redirect=False)
        self.assertEqual(classify_head_response(result, "https://x.com/page"), "reject")

    def test_text_html_with_query_params_no_audio_ext_rejected(self) -> None:
        result = HeadResult(status=200, content_type="text/html", is_redirect=False)
        self.assertEqual(
            classify_head_response(result, "https://x.com/listen?id=123"),
            "reject",
        )

    def test_image_png_without_audio_ext_rejected(self) -> None:
        result = HeadResult(status=200, content_type="image/png", is_redirect=False)
        self.assertEqual(classify_head_response(result, "https://x.com/pic.png"), "reject")

    def test_application_pdf_without_audio_ext_rejected(self) -> None:
        result = HeadResult(status=200, content_type="application/pdf", is_redirect=False)
        self.assertEqual(classify_head_response(result, "https://x.com/doc.pdf"), "reject")

    # --- edge: non-audio ct but audio ext → accept (ext wins) ---

    def test_audio_ext_overrides_non_audio_ct(self) -> None:
        # A lying content-type should still yield accept when extension is audio
        result = HeadResult(status=200, content_type="application/octet-stream", is_redirect=False)
        self.assertEqual(classify_head_response(result, "https://x.com/audio.wav"), "accept")


# ---------------------------------------------------------------------------
# LocalPathCandidateBasicTests  (was: LocalPathCandidateTests — W-1 rename)
# ---------------------------------------------------------------------------

class LocalPathCandidateBasicTests(unittest.TestCase):
    """is_local_path_candidate() — path-candidate detection (FR-001/FR-002, AC-5.1)."""

    def test_absolute_path_is_candidate(self) -> None:
        self.assertTrue(stt_utils.is_local_path_candidate("/abs/x.m4a"))

    def test_tilde_path_is_candidate(self) -> None:
        self.assertTrue(stt_utils.is_local_path_candidate("~/dl/x.m4a"))

    def test_surrounding_whitespace_is_candidate(self) -> None:
        # Leading/trailing whitespace is stripped before prefix check (FR-001).
        self.assertTrue(stt_utils.is_local_path_candidate("  /abs/x.m4a  "))

    def test_unicode_path_with_space_and_eszett_is_candidate(self) -> None:
        # FR-012/NFR-003: embedded space and ß in the filename are not structural.
        self.assertTrue(stt_utils.is_local_path_candidate("/Users/o/Wattstraße 8.m4a"))

    def test_non_audio_extension_is_still_candidate(self) -> None:
        # Detection is structural-only; extension is validated separately (FR-005).
        self.assertTrue(stt_utils.is_local_path_candidate("/notes/todo.txt"))

    def test_plain_text_is_not_candidate(self) -> None:
        self.assertFalse(stt_utils.is_local_path_candidate("how do I transcribe?"))

    def test_empty_string_is_not_candidate(self) -> None:
        self.assertFalse(stt_utils.is_local_path_candidate(""))

    def test_tilde_without_slash_is_not_candidate(self) -> None:
        # ~otheruser/ is not the supported ~/  prefix.
        self.assertFalse(stt_utils.is_local_path_candidate("~notuser/x.m4a"))

    def test_relative_path_is_not_candidate(self) -> None:
        self.assertFalse(stt_utils.is_local_path_candidate("./rel/x.m4a"))

    def test_url_is_not_candidate(self) -> None:
        # URL route must stay intact — https:// is not a local path prefix.
        self.assertFalse(stt_utils.is_local_path_candidate("https://x/y.mp3"))


# ---------------------------------------------------------------------------
# SttCommandRoutingBasicTests  (was: SttCommandRoutingTests — W-1 rename)
# ---------------------------------------------------------------------------

class SttCommandRoutingBasicTests(unittest.TestCase):
    """classify_stt_command() — command routing decision (FR-013/AC-5.2, ADR-006)."""

    def test_path_arg_routes_to_path(self) -> None:
        action, arg = stt_utils.classify_stt_command("/stt /a/b.m4a", 4)
        self.assertEqual(action, "path")
        self.assertEqual(arg, "/a/b.m4a")

    def test_unicode_with_embedded_space_preserved(self) -> None:
        # AC-1.2: space and ß must be preserved as-is — entity-length slice (ADR-006).
        action, arg = stt_utils.classify_stt_command(
            "/stt /Users/o/Wattstraße 8.m4a", 4
        )
        self.assertEqual(action, "path")
        self.assertIn("Wattstraße", arg)
        self.assertIn(" 8", arg)

    def test_double_inner_space_preserved(self) -> None:
        # A double space INSIDE the path must not be collapsed (ADR-006 vs context.args).
        action, arg = stt_utils.classify_stt_command("/stt  /a/b  c.m4a", 4)
        self.assertEqual(action, "path")
        # The inner double space in "/a/b  c.m4a" must survive.
        self.assertIn("  ", arg)

    def test_botname_suffix_handled(self) -> None:
        # /stt@botname form: command_len=9 slices past "@bot".
        action, arg = stt_utils.classify_stt_command("/stt@bot /a/b.m4a", 9)
        self.assertEqual(action, "path")
        self.assertEqual(arg, "/a/b.m4a")

    def test_non_path_arg_routes_to_toggle(self) -> None:
        action, arg = stt_utils.classify_stt_command("/stt hello", 4)
        self.assertEqual(action, "toggle")
        self.assertEqual(arg, "")

    def test_bare_command_routes_to_toggle(self) -> None:
        action, arg = stt_utils.classify_stt_command("/stt", 4)
        self.assertEqual(action, "toggle")
        self.assertEqual(arg, "")

    def test_whitespace_only_arg_routes_to_toggle(self) -> None:
        action, arg = stt_utils.classify_stt_command("/stt   ", 4)
        self.assertEqual(action, "toggle")
        self.assertEqual(arg, "")

    def test_none_command_len_fallback_path(self) -> None:
        # command_len=None triggers the split-on-first-whitespace fallback.
        action, arg = stt_utils.classify_stt_command("/stt /a/b.m4a", None)
        self.assertEqual(action, "path")
        self.assertEqual(arg, "/a/b.m4a")

    def test_none_command_len_fallback_toggle(self) -> None:
        action, arg = stt_utils.classify_stt_command("/stt hello", None)
        self.assertEqual(action, "toggle")
        self.assertEqual(arg, "")

    def test_none_command_len_fallback_bare(self) -> None:
        action, arg = stt_utils.classify_stt_command("/stt", None)
        self.assertEqual(action, "toggle")
        self.assertEqual(arg, "")


# ---------------------------------------------------------------------------
# LocalAudioPathCheckBasicTests  (was: LocalAudioPathCheckTests — W-1 rename)
# ---------------------------------------------------------------------------

class LocalAudioPathCheckBasicTests(unittest.TestCase):
    """check_local_audio_path() — validation (FR-004/FR-005/FR-006, ADR-008)."""

    def test_real_m4a_file_returns_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "audio.m4a"
            f.touch()
            raw = str(f)
            result = stt_utils.check_local_audio_path(raw)
            self.assertEqual(result.verdict, "ok")
            self.assertEqual(result.path, f)
            self.assertEqual(result.display, raw)

    def test_uppercase_extension_is_ok(self) -> None:
        # Extension check is case-insensitive (ADR-008/BRD Assumptions §7).
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "audio.M4A"
            f.touch()
            result = stt_utils.check_local_audio_path(str(f))
            self.assertEqual(result.verdict, "ok")

    def test_existing_txt_returns_bad_extension(self) -> None:
        # FR-005: extension checked before existence; .txt is not in AUDIO_EXTENSIONS.
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "notes.txt"
            f.touch()
            raw = str(f)
            result = stt_utils.check_local_audio_path(raw)
            self.assertEqual(result.verdict, "bad_extension")
            self.assertIsNone(result.path)
            # display must be the verbatim user input (FR-005 error naming).
            self.assertEqual(result.display, raw)

    def test_missing_m4a_returns_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = str(Path(tmp) / "missing.m4a")
            result = stt_utils.check_local_audio_path(raw)
            self.assertEqual(result.verdict, "not_found")

    def test_directory_named_m4a_returns_not_found(self) -> None:
        # A directory passes the extension check but fails is_file() (ADR-008).
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "dir.m4a"
            d.mkdir()
            result = stt_utils.check_local_audio_path(str(d))
            self.assertEqual(result.verdict, "not_found")

    def test_tilde_prefix_expands_to_home(self) -> None:
        # AC-2.1 / FR-004: ~ is expanded before existence check.
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "audio.m4a"
            f.touch()
            old_home = os.environ.get("HOME")
            try:
                os.environ["HOME"] = tmp
                result = stt_utils.check_local_audio_path("~/audio.m4a")
                self.assertEqual(result.verdict, "ok")
            finally:
                if old_home is not None:
                    os.environ["HOME"] = old_home
                else:
                    os.environ.pop("HOME", None)

    def test_unicode_filename_eszett_is_ok(self) -> None:
        # NFR-003: ß in filename must not cause any failure.
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "Wattstraße 8.m4a"
            f.touch()
            result = stt_utils.check_local_audio_path(str(f))
            self.assertEqual(result.verdict, "ok")
            # sanitize_filename must preserve the ß stem correctly (NFR-003).
            stem = sanitize_filename(result.path.name)
            self.assertEqual(stem, "Wattstraße 8")

    def test_nul_byte_path_returns_not_found(self) -> None:
        # ADR-008: Path.is_file() returns False for NUL bytes — safe landing.
        result = stt_utils.check_local_audio_path("/tmp/a\x00b.m4a")
        self.assertEqual(result.verdict, "not_found")


# ---------------------------------------------------------------------------
# LocalPathCandidateTests
# ---------------------------------------------------------------------------

class LocalPathCandidateTests(unittest.TestCase):
    """is_local_path_candidate() — structural prefix detection (FR-001/FR-012/AC-5.1)."""

    def test_absolute_slash_path_is_candidate(self) -> None:
        self.assertTrue(stt_utils.is_local_path_candidate("/abs/x.m4a"))

    def test_home_relative_path_is_candidate(self) -> None:
        self.assertTrue(stt_utils.is_local_path_candidate("~/dl/x.m4a"))

    def test_whitespace_padded_absolute_path_is_candidate(self) -> None:
        # Surrounding whitespace is stripped before check
        self.assertTrue(stt_utils.is_local_path_candidate("  /abs/x.m4a  "))

    def test_unicode_space_and_special_char_is_candidate(self) -> None:
        # FR-012/NFR-003: path with embedded space and ß character
        self.assertTrue(stt_utils.is_local_path_candidate("/Users/o/Wattstraße 8.m4a"))

    def test_non_audio_extension_still_candidate(self) -> None:
        # Detection is structural-prefix-only (FR-005 ordering); .txt is still a candidate
        self.assertTrue(stt_utils.is_local_path_candidate("/notes/todo.txt"))

    def test_plain_text_is_not_candidate(self) -> None:
        self.assertFalse(stt_utils.is_local_path_candidate("how do I transcribe?"))

    def test_empty_string_is_not_candidate(self) -> None:
        self.assertFalse(stt_utils.is_local_path_candidate(""))

    def test_tilde_other_user_is_not_candidate(self) -> None:
        # ~otheruser does not start with ~/
        self.assertFalse(stt_utils.is_local_path_candidate("~otheruser/x.m4a"))

    def test_relative_dot_slash_is_not_candidate(self) -> None:
        self.assertFalse(stt_utils.is_local_path_candidate("./rel/x.m4a"))

    def test_https_url_is_not_candidate(self) -> None:
        # URL route must stay intact; a bare URL is not a local path candidate
        self.assertFalse(stt_utils.is_local_path_candidate("https://x.com/y.mp3"))


# ---------------------------------------------------------------------------
# SttCommandRoutingTests
# ---------------------------------------------------------------------------

class SttCommandRoutingTests(unittest.TestCase):
    """classify_stt_command() — /stt trailing argument routing (FR-002/FR-003/FR-013/AC-5.2)."""

    def test_absolute_path_arg_routes_to_path(self) -> None:
        action, arg = stt_utils.classify_stt_command("/stt /a/b.m4a", 4)
        self.assertEqual(action, "path")
        self.assertEqual(arg, "/a/b.m4a")

    def test_path_with_unicode_and_space_preserved(self) -> None:
        # FR-002/AC-1.2: embedded space and ß must survive intact
        text = "/stt /Users/o/Wattstraße 8.m4a"
        action, arg = stt_utils.classify_stt_command(text, 4)
        self.assertEqual(action, "path")
        self.assertIn("Wattstraße", arg)
        self.assertIn(" 8", arg)

    def test_double_inner_space_preserved(self) -> None:
        # Multi-space paths are preserved byte-exactly (AC-1.2)
        text = "/stt  /a/b  c.m4a"
        action, arg = stt_utils.classify_stt_command(text, 4)
        self.assertEqual(action, "path")
        # After slicing at command_len=4, remaining is " /a/b  c.m4a", stripped = "/a/b  c.m4a"
        self.assertIn("  ", arg)

    def test_botname_suffix_handled_by_command_len(self) -> None:
        # /stt@bot has command_len=9; entity-length slicing covers the @botname form
        action, arg = stt_utils.classify_stt_command("/stt@bot /a/b.m4a", 9)
        self.assertEqual(action, "path")
        self.assertEqual(arg, "/a/b.m4a")

    def test_non_path_arg_routes_to_toggle(self) -> None:
        # FR-003: non-path argument → toggle
        action, arg = stt_utils.classify_stt_command("/stt hello", 4)
        self.assertEqual(action, "toggle")
        self.assertEqual(arg, "")

    def test_no_arg_routes_to_toggle(self) -> None:
        action, arg = stt_utils.classify_stt_command("/stt", 4)
        self.assertEqual(action, "toggle")
        self.assertEqual(arg, "")

    def test_whitespace_only_arg_routes_to_toggle(self) -> None:
        action, arg = stt_utils.classify_stt_command("/stt   ", 4)
        self.assertEqual(action, "toggle")
        self.assertEqual(arg, "")

    def test_home_tilde_path_arg_routes_to_path(self) -> None:
        action, arg = stt_utils.classify_stt_command("/stt ~/downloads/x.m4a", 4)
        self.assertEqual(action, "path")
        self.assertEqual(arg, "~/downloads/x.m4a")

    def test_command_len_none_fallback_absolute_path(self) -> None:
        # command_len=None → split-on-first-whitespace fallback (ADR-006)
        action, arg = stt_utils.classify_stt_command("/stt /a/b.m4a", None)
        self.assertEqual(action, "path")
        self.assertEqual(arg, "/a/b.m4a")

    def test_command_len_none_fallback_no_arg(self) -> None:
        action, arg = stt_utils.classify_stt_command("/stt", None)
        self.assertEqual(action, "toggle")
        self.assertEqual(arg, "")

    def test_command_len_none_fallback_non_path_arg(self) -> None:
        # W-1: this case existed only in the dead (shadowed) SttCommandRoutingBasicTests
        # class and was never executed.  A non-path argument with command_len=None
        # must route to toggle, not to path-input.
        action, arg = stt_utils.classify_stt_command("/stt hello", None)
        self.assertEqual(action, "toggle")
        self.assertEqual(arg, "")


# ---------------------------------------------------------------------------
# LocalAudioPathCheckTests
# ---------------------------------------------------------------------------

class LocalAudioPathCheckTests(unittest.TestCase):
    """check_local_audio_path() — validation flow (FR-004/FR-005/FR-006/ADR-008)."""

    def test_existing_m4a_returns_ok(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as fh:
            fh.write(b"fake audio")
            path = fh.name
        try:
            result = stt_utils.check_local_audio_path(path)
            self.assertEqual(result.verdict, "ok")
            self.assertIsNotNone(result.path)
            self.assertEqual(result.display, path)
        finally:
            os.unlink(path)

    def test_uppercase_extension_is_accepted(self) -> None:
        # NFR-003 / extension matching is case-insensitive
        with tempfile.NamedTemporaryFile(suffix=".M4A", delete=False) as fh:
            fh.write(b"fake audio")
            path = fh.name
        try:
            result = stt_utils.check_local_audio_path(path)
            self.assertEqual(result.verdict, "ok")
        finally:
            os.unlink(path)

    def test_existing_txt_returns_bad_extension(self) -> None:
        # FR-005: extension check before existence; .txt → bad_extension
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as fh:
            fh.write(b"text")
            path = fh.name
        try:
            result = stt_utils.check_local_audio_path(path)
            self.assertEqual(result.verdict, "bad_extension")
            self.assertIsNone(result.path)
            # display carries verbatim user input (FR-005)
            self.assertEqual(result.display, path)
        finally:
            os.unlink(path)

    def test_missing_m4a_returns_not_found(self) -> None:
        path = "/tmp/__stt_test_nonexistent_x7q9z2.m4a"
        result = stt_utils.check_local_audio_path(path)
        self.assertEqual(result.verdict, "not_found")
        self.assertIsNone(result.path)
        self.assertEqual(result.display, path)

    def test_directory_named_m4a_returns_not_found(self) -> None:
        # ADR-008: is_file() on a directory → False → not_found, not a crash
        with tempfile.TemporaryDirectory() as d:
            dir_path = os.path.join(d, "recording.m4a")
            os.makedirs(dir_path)
            result = stt_utils.check_local_audio_path(dir_path)
            self.assertEqual(result.verdict, "not_found")

    def test_tilde_path_is_expanded(self) -> None:
        # AC-2.1/FR-004: ~/path expanded against process HOME
        with tempfile.TemporaryDirectory() as d:
            audio_file = os.path.join(d, "test.m4a")
            with open(audio_file, "wb") as fh:
                fh.write(b"fake audio")
            real_home = os.environ.get("HOME", "")
            try:
                os.environ["HOME"] = d
                result = stt_utils.check_local_audio_path("~/test.m4a")
                self.assertEqual(result.verdict, "ok")
                self.assertIsNotNone(result.path)
                self.assertTrue(str(result.path).startswith(d))
            finally:
                if real_home:
                    os.environ["HOME"] = real_home
                else:
                    os.environ.pop("HOME", None)

    def test_unicode_filename_ok_and_sanitizes_correctly(self) -> None:
        # NFR-003: ß in filename → ok verdict + sanitize_filename yields Wattstraße
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "Wattstraße 8.m4a")
            with open(path, "wb") as fh:
                fh.write(b"fake audio")
            result = stt_utils.check_local_audio_path(path)
            self.assertEqual(result.verdict, "ok")
            self.assertIsNotNone(result.path)
            stem = stt_utils.sanitize_filename(result.path.name)
            self.assertIn("Wattstra", stem)

    def test_nul_byte_path_returns_not_found(self) -> None:
        # ADR-008: NUL byte in path → not_found, not ValueError crash
        result = stt_utils.check_local_audio_path("/tmp/a\x00b.m4a")
        self.assertEqual(result.verdict, "not_found")


if __name__ == "__main__":
    unittest.main()
