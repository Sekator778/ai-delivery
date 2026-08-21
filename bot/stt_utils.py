"""stt_utils.py — pure, stdlib-only helpers for the /stt feature.

No Telegram, no aiohttp, no external dependencies. All handler I/O stays
in bot.py; this module contains only testable decision logic and filesystem
primitives (FR-019, ADR-002).
"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Accepted audio filename extensions (FR-001, FR-004).
# Lowercase, with leading dot. Shared by attachment detection and URL
# pre-validation — the same list for both (BRD Assumptions §6).
AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".m4a", ".mp3", ".ogg", ".wav", ".aiff", ".flac"}
)


# ---------------------------------------------------------------------------
# Local-path helpers (FR-001/FR-002, ADR-006/ADR-008)
# ---------------------------------------------------------------------------

# Structural prefixes that identify a candidate local audio file path.
# Detection is prefix-only; extension is validated separately (FR-005/ADR-008).
LOCAL_PATH_PREFIXES: tuple[str, ...] = ("/", "~/")


def is_local_path_candidate(text: str) -> bool:
    """Return True if *text* (after stripping) looks like a local file path.

    Detection is structural — startswith "/" or "~/" — with no extension or
    existence check (FR-001/FR-002). Extension is validated separately by
    check_local_audio_path (FR-005/ADR-008). Returns False for bare "~" and
    "~user/…" (only "~/" is the supported home-relative prefix).
    """
    return text.strip().startswith(LOCAL_PATH_PREFIXES)


def split_command_argument(text: str, command_len: int | None = None) -> str:
    """Extract the trailing argument text after a Telegram bot command token.

    ADR-006: when *command_len* is the length of the bot_command entity
    (e.g. 4 for "/stt", 9 for "/stt@bot"), slice *text* at that boundary and
    strip surrounding whitespace — this preserves ALL embedded spaces in the
    remainder. Falls back to splitting on the first whitespace run when
    *command_len* is None (e.g. in unit tests with plain strings).

    >>> split_command_argument("/stt /a/b  c.m4a", 4)
    '/a/b  c.m4a'
    >>> split_command_argument("/stt@bot /a/b.m4a", 9)
    '/a/b.m4a'
    >>> split_command_argument("/stt", 4)
    ''
    """
    if command_len is not None:
        return text[command_len:].strip()
    # Fallback: split on the first whitespace run (two-token form: cmd + rest).
    parts = text.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def classify_stt_command(
    text: str, command_len: int | None = None
) -> tuple[str, str]:
    """Classify a /stt invocation as a path input or a mode toggle.

    Returns:
        ("path", raw_arg)  — trailing text is a local path candidate (FR-002).
        ("toggle", "")     — no argument, whitespace-only, or non-path arg (FR-003).

    ADR-006: argument extracted by entity length, not context.args, to preserve
    embedded spaces (AC-1.2). ADR-011: this function is called AFTER the
    reply-audio branch so it never overrides that branch.
    """
    arg = split_command_argument(text, command_len)
    if is_local_path_candidate(arg):
        return ("path", arg)
    return ("toggle", "")


class LocalPathCheck(NamedTuple):
    """Result of check_local_audio_path() (ADR-008).

    verdict: one of "ok", "bad_extension", "not_found".
    path:    expanded Path when verdict == "ok"; None otherwise.
    display: verbatim user-supplied text for error messages (FR-005/FR-006).
    """

    verdict: str
    path: "Path | None"
    display: str


def check_local_audio_path(raw: str) -> LocalPathCheck:
    """Validate a candidate local audio file path.

    Validation order (ADR-008/FR-004/FR-005/FR-006):
      1. expanduser  — resolves "~/" prefix (FR-004).
      2. extension   — checked BEFORE existence (FR-005, case-insensitive).
      3. is_file()   — False for missing paths, directories, and NUL-byte
                       inputs (ADR-008 / NFR-005 safety property).

    Returns a LocalPathCheck NamedTuple with verdict in {"ok","bad_extension",
    "not_found"} and display set to the verbatim input for error messages.
    """
    display = raw.strip()
    expanded = os.path.expanduser(display)
    p = Path(expanded)

    # FR-005: extension check first, case-insensitive, no existence stat needed.
    ext = os.path.splitext(expanded)[1].lower()
    if ext not in AUDIO_EXTENSIONS:
        return LocalPathCheck(verdict="bad_extension", path=None, display=display)

    # FR-006: is_file() handles missing paths, directories, and NUL-byte inputs
    # (Path("/tmp/a\0b").is_file() → False on CPython, verified; ADR-008).
    if not p.is_file():
        return LocalPathCheck(verdict="not_found", path=None, display=display)

    return LocalPathCheck(verdict="ok", path=p, display=display)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def is_bare_url(text: str) -> bool:
    """Return True if text (after stripping) is a single http(s) URL and nothing else.

    FR-002: a plain text message consisting solely of a single http(s) URL.
    FR-003: URL together with other text → False.
    NFR-007: only http and https schemes; ftp, file, etc. → False.
    """
    stripped = text.strip()
    # Any embedded whitespace means it's not a bare URL
    if " " in stripped or "\n" in stripped or "\t" in stripped:
        return False
    return stripped.startswith(("http://", "https://"))


# ---------------------------------------------------------------------------
# HEAD pre-validation helpers
# ---------------------------------------------------------------------------

class HeadResult(NamedTuple):
    """Simplified result of an HTTP HEAD request for URL pre-validation."""

    status: int | None       # HTTP status code; None means timeout/connection error
    content_type: str | None  # Value of Content-Type header; None if absent
    is_redirect: bool        # True when status is 3xx (redirect)


def classify_head_response(result: HeadResult, url: str) -> str:
    """Classify a HEAD response for an audio URL candidate.

    Returns one of:
      'accept'      — proceed directly to download (FR-004a)
      'inconclusive' — proceed to download anyway, rely on whisper-server (FR-004b)
      'reject'      — do not download, tell the user the URL is not audio (FR-004c)

    Decision logic (FR-004):
      a. audio/ Content-Type OR audio filename extension → accept
      b. timeout/error (status None), redirect, or missing Content-Type → inconclusive
      c. clearly non-audio Content-Type AND no audio extension → reject
    """
    # Rule b — timeout / connection error
    if result.status is None:
        return "inconclusive"

    # Rule b — redirect (3xx); HEAD redirect != the real resource Content-Type
    if result.is_redirect:
        return "inconclusive"

    ct = (result.content_type or "").lower().strip()

    # Rule a — audio Content-Type
    if ct.startswith("audio/"):
        return "accept"

    # Rule a — audio extension in the URL path (ignore query string)
    path_part = url.split("?")[0].rstrip("/")
    last_segment = path_part.split("/")[-1] if "/" in path_part else path_part
    ext = os.path.splitext(last_segment)[1].lower()
    if ext in AUDIO_EXTENSIONS:
        return "accept"

    # Rule b — no Content-Type header present (empty string or None treated same)
    if not ct:
        return "inconclusive"

    # Rule c — Content-Type is present and clearly non-audio, no audio extension
    return "reject"


# ---------------------------------------------------------------------------
# Filename sanitization
# ---------------------------------------------------------------------------

def sanitize_filename(name: str, max_len: int = 200) -> str:
    """Return a safe base name stem (no extension) derived from *name*.

    Strips path components, control characters, leading dots/spaces, and
    caps length. Falls back to "audio" if nothing usable remains.

    Security (OWASP ASVS / path traversal): only the basename is kept;
    the result is never an absolute path and contains no path separators.
    """
    # Keep only the last path component (handles / and os-specific sep)
    name = os.path.basename(name)
    # Work with the stem (drop extension)
    stem, _ = os.path.splitext(name)
    # Normalize unicode (decompose composed chars → canonical form)
    stem = unicodedata.normalize("NFKD", stem)
    # Remove control characters (NUL, LF, CR, TAB, etc.) first —
    # unicodedata category "C*" covers all control/format/surrogate chars.
    stem = "".join(ch for ch in stem if unicodedata.category(ch)[0] != "C")
    # Replace any character that is not a word char (\w = [a-zA-Z0-9_] +
    # unicode word chars), plain space, hyphen, or dot with underscore.
    stem = re.sub(r"[^\w \-.]", "_", stem)
    # Strip leading/trailing dots and spaces (hidden-file / path-confusion guard)
    stem = stem.strip(". ")
    if not stem:
        stem = "audio"
    return stem[:max_len]


# ---------------------------------------------------------------------------
# Transcript file path derivation
# ---------------------------------------------------------------------------

def derive_transcript_path(output_dir: Path, stem: str) -> Path:
    """Find the next collision-free .txt path in *output_dir* for *stem*.

    First try ``<stem>.txt``; on collision try ``<stem>-1.txt``,
    ``<stem>-2.txt``, … The collision detection uses an exclusive
    ``O_CREAT | O_EXCL`` open — the filesystem arbitrates, never a
    stat-then-write race (NFR-006, FR-011).

    Returns the Path of the successfully created (empty) file.
    The caller is responsible for writing the transcript content.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Try the bare stem first
    candidate = output_dir / f"{stem}.txt"
    try:
        fd = os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return candidate
    except FileExistsError:
        pass

    # Increment suffix until we win the exclusive create
    i = 1
    while True:
        candidate = output_dir / f"{stem}-{i}.txt"
        try:
            fd = os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return candidate
        except FileExistsError:
            i += 1
