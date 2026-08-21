"""Tests for whisper-server format-handling fix (FR-020 [Should], AC-9.2).

Tests the suffix-derivation helper (_derive_upload_suffix) which replaces the
old hardcoded '.ogg' temp-file suffix. The helper is a pure function and can be
imported directly without a running FastAPI app or whisper model (FR-020's
"feasible without heavyweight fixtures" condition).

If the 'fastapi' package is not installed in the host test environment the
entire module is still importable but the tests are skipped with an explanatory
message — per AC-9.2 ("if not feasible, the constraint that made it infeasible
is documented alongside the test suite").
"""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Feasibility check — fastapi required only for the app-level import.
# _derive_upload_suffix itself uses only stdlib; we can test it regardless.
# ---------------------------------------------------------------------------

_FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None
_MULTIPART_AVAILABLE = importlib.util.find_spec("python_multipart") is not None

# Add the server directory to sys.path so we can import server.py directly.
_SERVER_DIR = str(REPO_ROOT / "services" / "stacks" / "voice" / "whisper-server")

# Both fastapi AND python-multipart are required to import server.py
# (FastAPI checks for python-multipart when registering File/Form endpoints).
_SERVER_IMPORTABLE = _FASTAPI_AVAILABLE and _MULTIPART_AVAILABLE


def _import_derive_suffix():
    """Import _derive_upload_suffix from server.py.

    Requires fastapi and python-multipart installed in the test environment.
    """
    if _SERVER_DIR not in sys.path:
        sys.path.insert(0, _SERVER_DIR)
    import server  # noqa: PLC0415
    return server._derive_upload_suffix  # noqa: SLF001


_SKIP_REASON = (
    "fastapi and/or python-multipart are not installed in the host test "
    "environment — whisper-server cannot be imported. "
    "Install both: pip install fastapi python-multipart. "
    "The suffix-derivation logic itself is pure stdlib and the test cases "
    "are documented here for reference (AC-9.2)."
)


@unittest.skipUnless(_SERVER_IMPORTABLE, _SKIP_REASON)
class WhisperServerSuffixTests(unittest.TestCase):
    """_derive_upload_suffix() maps upload filenames to whitelisted suffixes.

    FR-013/ADR-004: the server must no longer assume .ogg for every upload.
    """

    def setUp(self) -> None:
        self._derive = _import_derive_suffix()

    def test_m4a_extension_preserved(self) -> None:
        self.assertEqual(self._derive("recording.m4a"), ".m4a")

    def test_mp3_extension_preserved(self) -> None:
        self.assertEqual(self._derive("podcast.mp3"), ".mp3")

    def test_ogg_extension_preserved(self) -> None:
        self.assertEqual(self._derive("voice.ogg"), ".ogg")

    def test_wav_extension_preserved(self) -> None:
        self.assertEqual(self._derive("audio.wav"), ".wav")

    def test_aiff_extension_preserved(self) -> None:
        self.assertEqual(self._derive("track.aiff"), ".aiff")

    def test_flac_extension_preserved(self) -> None:
        self.assertEqual(self._derive("lossless.flac"), ".flac")

    def test_unknown_extension_falls_back_to_bin(self) -> None:
        self.assertEqual(self._derive("file.mp4"), ".bin")

    def test_no_extension_falls_back_to_bin(self) -> None:
        self.assertEqual(self._derive("audiostream"), ".bin")

    def test_none_filename_falls_back_to_bin(self) -> None:
        self.assertEqual(self._derive(None), ".bin")

    def test_empty_filename_falls_back_to_bin(self) -> None:
        self.assertEqual(self._derive(""), ".bin")

    def test_uppercase_extension_normalized(self) -> None:
        # Extension matching must be case-insensitive
        self.assertEqual(self._derive("AUDIO.MP3"), ".mp3")

    def test_path_with_directory_component(self) -> None:
        # Client-supplied filename may carry directory — only ext matters
        result = self._derive("/tmp/uploads/voice_note.ogg")
        self.assertEqual(result, ".ogg")

    def test_dotfile_no_useful_extension(self) -> None:
        # ".hidden" → ext="" → fallback
        self.assertEqual(self._derive(".hidden"), ".bin")

    def test_double_extension_uses_last(self) -> None:
        # os.path.splitext(".tar.gz") → ('.tar', '.gz') — .gz not audio → .bin
        result = self._derive("archive.tar.gz")
        self.assertEqual(result, ".bin")


if __name__ == "__main__":
    unittest.main()
