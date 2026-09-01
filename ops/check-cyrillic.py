#!/usr/bin/env python3
"""Cyrillic scanner for the public export — the enforcement half of CLAUDE.md §2.

CLAUDE.md §2 has always required English for public-facing artifacts. Russian
still reached the public mirror on the first resumed publish (2026-09-01),
because nothing checked. This is the check.

Why Python and not a `grep -P` over a Cyrillic character class, which is the
obvious one-liner:

  1. It is byte-oriented unless the locale says otherwise. With LANG unset,
     PCRE compiles the class into a *byte* range, and every non-ASCII
     character matches it — em dashes, CJK, emoji. Measured on bot/bot.py:
     379 "hits" against 211 real ones, and this repository's prose is full of
     em dashes, so the gate would have been red on almost everything.
  2. `scripts/publish-public.sh` runs on the operator's macOS machine, where
     grep is BSD grep: no `-P` at all, and no `C.UTF-8` locale to force.

Decoding to str first makes the class mean code points, which is what it is
supposed to mean, and behaves identically on macOS and Linux.

Binary files are skipped rather than reported. A file that does not decode as
UTF-8 has no "lines with Cyrillic" to speak of, and treating one as a finding
would mean the first PNG added to the export takes the publish down —
docs/demo.gif already matched a byte-level scan.

Exit codes:
  0  no Cyrillic outside the allowlist
  1  usage error
  2  findings (the caller decides what that means; publish-public.sh maps it
     to its own exit 3, "gate finding")
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Code points, not bytes: U+0430..U+044F (a-ya), U+0410..U+042F (A-YA),
# U+0451 (yo), U+0401 (YO).
#
# Written as escapes on purpose. Spelling the class with literal letters would
# make this file its own first finding, and the one file that must never need
# an allowlist entry is the scanner enforcing the allowlist.
CYRILLIC = re.compile("[\u0430-\u044f\u0410-\u042f\u0451\u0401]")

# Files where Cyrillic is CORRECT and must not be flagged.
#
# CLAUDE.md §2: "user-facing bot messages (visible in Telegram) — Russian, per
# product decision". These files carry those strings, or assert them. Anything
# added here needs the same one-line justification: which Russian text lives in
# it and why it is product surface rather than a documentation slip.
#
# Scope note, deliberate: this is a FILE-level allowlist, so a Russian *comment*
# added to one of these files would not be caught. Line-level classification
# would mean parsing Python to tell a string literal from a comment, in a gate
# that must also read YAML and Markdown. The sweep that accompanied this gate
# translated the comments in these files by hand; the residual risk is that a
# future one slips in, which is a smaller problem than a gate nobody can trust.
ALLOWLIST = {
    # Telegram-facing bot surface
    "bot/bot.py":                       "Telegram command replies and keyboards",
    "bot/budget_buttons.py":            "budget prompt buttons shown in Telegram",
    "dispatcher/budget_gate.py":        "budget stop message sent to the operator",
    "dispatcher/clarify.py":            "clarification questions asked in Telegram",
    "dispatcher/watcher.py":            "recovery and stall notifications",
    "dispatcher/post_pipeline.py":      "post-pipeline result message",
    "dispatcher/control_loop.py":       "verdict wording surfaced to the operator",
    "dispatcher/auto_loop.py":          "auto-loop progress messages",
    "dispatcher/stage_runner_agent.py": "stage progress messages",
    # Live prompt and product data, not documentation
    "meta/CLAUDE.md":                   "orchestrator prompt: Russian trigger phrases it must recognise and reply templates it sends",
    "services/stacks/voice/silero-server/README.md": "Russian TTS stress-mark examples — the syntax being documented",
    # Tests that pin the wording of those messages
    "tests/test_telegram_formatting.py": "asserts the Russian Telegram formatting",
    "tests/test_review_surfacing.py":    "asserts the Russian review summary",
    "tests/test_stt_local_source.py":    "asserts Russian speech-to-text fixtures",
    "tests/test_stt_utils.py":           "asserts Russian speech-to-text fixtures",
    "tests/test_runner_recovery.py":     "asserts the Russian recovery notice",
    "tests/test_publish_public.py":       "builds Cyrillic fixtures to exercise this very gate",
}


def scan_file(path: Path, rel: str) -> list[tuple[int, str]]:
    """Return [(line number, line)] containing Cyrillic. Binary files: []."""
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        # Not text (or unreadable): nothing to say about its prose.
        return []
    return [
        (n, line.rstrip())
        for n, line in enumerate(text.splitlines(), 1)
        if CYRILLIC.search(line)
    ]


def scan_tree(root: Path) -> dict[str, list[tuple[int, str]]]:
    """Scan every regular file under root, keyed by path relative to it."""
    findings: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".git/"):
            continue
        if rel in ALLOWLIST:
            continue
        hits = scan_file(path, rel)
        if hits:
            findings[rel] = hits
    return findings


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Fail when Cyrillic appears outside the allowlist.",
    )
    ap.add_argument("root", help="directory to scan (the export tree)")
    ap.add_argument("--max-lines", type=int, default=5,
                    help="how many offending lines to print per file (default 5)")
    args = ap.parse_args(argv[1:])

    root = Path(args.root)
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 1

    findings = scan_tree(root)
    if not findings:
        return 0

    total = sum(len(v) for v in findings.values())
    print(
        f"Cyrillic found in {len(findings)} file(s), {total} line(s), outside "
        f"the allowlist in {Path(__file__).name}:",
        file=sys.stderr,
    )
    for rel, hits in findings.items():
        print(f"  {rel}  ({len(hits)} line(s))", file=sys.stderr)
        for n, line in hits[: args.max_lines]:
            print(f"    {n}: {line[:100]}", file=sys.stderr)
        if len(hits) > args.max_lines:
            print(f"    ... {len(hits) - args.max_lines} more", file=sys.stderr)
    print(
        "\nPublic artifacts are English (CLAUDE.md §2). Translate them, or — if "
        "this is Telegram-facing product text — add the file to ALLOWLIST with "
        "the reason.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
