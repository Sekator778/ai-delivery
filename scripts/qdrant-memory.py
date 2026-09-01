#!/usr/bin/env python3
"""Export and re-import the pipeline's semantic memory as plain JSONL.

WHY THIS EXISTS
    The Qdrant collection that `dispatcher/memory_inject.py` recalls from is
    the only copy of the pipeline's accumulated memory, it lives in a
    gitignored bind mount, and it is not portable: its on-disk form is RocksDB
    segments whose size is dominated by preallocation, and its vectors are
    meaningless without the exact embedding model that produced them.

    The payloads, though, are text. This script moves the text in and out of
    JSONL so memory can be committed, diffed, reviewed, and rebuilt anywhere:

        dump     Qdrant  -> JSONL   (payloads only; --with-vectors adds them)
        restore  JSONL   -> Qdrant  (re-embeds each record through TEI)
        stats    describe either side without changing anything

    Vectors are NOT exported by default, for the reason they never were: they
    are a derived artifact — 1024 floats per point that bloat the file, cannot
    be reviewed, and are invalid the moment the embedding model changes.
    Re-embedding on restore is cheap and consistent with whatever model is
    serving at the time.

    `--with-vectors` exists for one purpose (T13): the flat store that replaces
    Qdrant IS this file with its vectors, so `dispatcher/memory_flat.py` needs
    an export that carries them. Two consequences worth knowing before running
    it: the file grows by roughly 3 MB per 800 points, and it stops being
    reviewable in a diff. Keep the payload-only export as the committed,
    readable copy; write the vector export where MEMORY_FLAT_PATH points.

INFRASTRUCTURE
    Qdrant  (default http://127.0.0.1:6333) — dump and restore
    TEI     (default http://127.0.0.1:8087) — restore only

    Both are read from the same environment variables that memory_inject.py
    uses, so a shell configured for one is configured for the other:
    MEMORY_QDRANT_URL, MEMORY_TEI_URL, MEMORY_COLLECTION.

    Unlike memory_inject.py, this script does NOT degrade to a no-op. It is an
    operator tool: it reports failures and exits non-zero, because a dump that
    silently produced nothing is worse than no dump at all.

SECRET GATE
    `dump` refuses to write an export whose records contain credential shapes,
    and `purge` removes those records from the collection. This is load-
    bearing: the retired mem0 lifecycle hooks captured whole conversation
    turns, so the collection holds credentials that were merely mentioned in a
    session. They are a live exposure even without git, because recall injects
    matching records into stage prompts that go to third-party providers.

Usage:
    scripts/qdrant-memory.py dump    [--out PATH] [--exclude-flagged]
                                     [--with-vectors]
    scripts/qdrant-memory.py restore [--in PATH] [--dry-run] [--overwrite]
    scripts/qdrant-memory.py purge   [--flagged] [--yes]
    scripts/qdrant-memory.py stats   [--in PATH]

Exit codes:
    0  success
    1  usage error
    2  infrastructure unreachable or refused
    3  data error (empty dump, unusable records)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXPORT = REPO_ROOT / "memory-bank" / "semantic-export" / "meta_agent_mem.jsonl"
# The vector export is a different file with a different job: it IS the flat
# store dispatcher/memory_flat.py reads, so its name and shape are that
# module's contract, not a formatting preference.
DEFAULT_VECTOR_EXPORT = DEFAULT_EXPORT.with_name("meta_agent_mem.vectors.jsonl")

# TEI advertises max_client_batch_size=32; batching above it is rejected.
EMBED_BATCH = 32
SCROLL_PAGE = 256
UPSERT_BATCH = 64

# Payload keys that have held the human-readable text, newest convention
# first: `text` is what memory_inject.py write-back writes, `data`/`memory`
# are what the retired mem0 hooks wrote. Preserved verbatim on dump either
# way — this precedence only decides what gets re-embedded on restore.
TEXT_KEYS = ("text", "data", "memory", "content")

# Credential shapes that must never reach the export.
#
# This gate is not paranoia. The retired mem0 lifecycle hooks captured whole
# conversation turns into this collection, so it holds credentials that were
# merely *mentioned* in a session — verified on the first real dump of
# meta_agent_mem, which carried a Telegram bot token and three API keys across
# six legacy points. Without this gate a naive `dump` writes them straight
# into git.
#
# The same records are also a live exfiltration path independent of git:
# `recall()` injects top-K matches into ba/architect/developer prompts, and
# those stages run against third-party providers. Flagged points should be
# purged from the collection, not just filtered out of the file — see `purge`.
SECRET_PATTERNS = (
    ("telegram-bot-token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}")),
    ("sk-api-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("langsmith-key", re.compile(r"\blsv2[_A-Za-z0-9-]{20,}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("assigned-credential",
     re.compile(r"(?i)\b(api[_-]?key|token|secret|passwd|password)\b"
                r"\W{1,4}[A-Za-z0-9/+_-]{32,}")),
)


def flag_secrets(records: "list[dict]") -> "dict[str, list[str]]":
    """Map point id -> matched rule names, for every record whose serialized
    form contains a credential shape. Scans the whole record, not just the
    text key: a leaked value is just as harmful sitting in a metadata field."""
    flagged: dict[str, list[str]] = {}
    for record in records:
        blob = json.dumps(record, ensure_ascii=False)
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(blob):
                flagged.setdefault(str(record.get("id")), []).append(name)
    return flagged


def write_jsonl(path: Path, records: "list[dict]") -> None:
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            fh.write("\n")


def gitleaks_lines(path: Path) -> "dict[int, list[str]]":
    """Line numbers gitleaks flags in a JSONL file, mapped to rule ids.

    Layer 1 of the gate, mirroring scripts/publish-public.sh: the hand-written
    SECRET_PATTERNS above are not a superset of gitleaks' ruleset, and trying
    to make them one is a losing game. One record per line means a flagged
    line number maps straight back to a record.

    Returns {} if gitleaks is absent — callers must log that as a degraded
    scan, never as a clean one.
    """
    if not shutil.which("gitleaks"):
        return {}
    with tempfile.TemporaryDirectory() as tmpdir:
        report = Path(tmpdir) / "report.json"
        empty = Path(tmpdir) / "allow"
        empty.touch()
        subprocess.run(
            ["gitleaks", "dir", str(path), "--no-banner", "--redact",
             "--exit-code", "0", "--ignore-gitleaks-allow",
             "--gitleaks-ignore-path", str(empty),
             "--report-format", "json", "--report-path", str(report)],
            capture_output=True, check=False)
        if not report.exists():
            return {}
        try:
            findings = json.loads(report.read_text() or "[]")
        except json.JSONDecodeError:
            return {}
    lines: dict[int, list[str]] = {}
    for finding in findings or []:
        lineno = finding.get("StartLine")
        if isinstance(lineno, int):
            lines.setdefault(lineno, []).append(
                finding.get("RuleID") or "unknown")
    return lines


def gate(path: Path, records: "list[dict]") -> "dict[str, list[str]]":
    """Both layers, merged into {point id -> rule names}."""
    flagged = flag_secrets(records)
    hits = gitleaks_lines(path)
    for lineno, rules in hits.items():
        index = lineno - 1
        if 0 <= index < len(records):
            key = str(records[index].get("id"))
            flagged.setdefault(key, []).extend(f"gitleaks:{r}" for r in rules)
    return flagged


def gate_layers_note() -> str:
    return ("gitleaks + %d built-in patterns" % len(SECRET_PATTERNS)
            if shutil.which("gitleaks")
            else "%d built-in patterns ONLY — gitleaks not on PATH, scan is "
                 "degraded" % len(SECRET_PATTERNS))


def env(name: str, default: str) -> str:
    return os.environ.get(name) or default


def qdrant_url() -> str:
    return env("MEMORY_QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")


def tei_url() -> str:
    return env("MEMORY_TEI_URL", "http://127.0.0.1:8087").rstrip("/")


def die(msg: str, code: int = 2) -> "None":
    print(f"[qdrant-memory] FATAL: {msg}", file=sys.stderr)
    raise SystemExit(code)


def log(msg: str) -> None:
    print(f"[qdrant-memory] {msg}")


def http_json(url: str, body: "dict | None" = None, method: str = "POST",
              timeout: int = 30) -> "dict | list":
    """One JSON round trip. Raises on transport or HTTP error — callers decide
    what is fatal; nothing here is swallowed."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"cannot reach {url}: {exc}") from exc


# ---------------------------------------------------------------------------
# dump
# ---------------------------------------------------------------------------

def scroll_all(collection: str, with_vectors: bool = False) -> "list[dict]":
    """Every point in the collection, payloads only, following the cursor to
    the end. Qdrant returns `next_page_offset: null` on the last page."""
    base = f"{qdrant_url()}/collections/{collection}/points/scroll"
    points: list[dict] = []
    offset = None
    page = 0
    while True:
        body: dict = {"limit": SCROLL_PAGE, "with_payload": True,
                      "with_vector": with_vectors}
        if offset is not None:
            body["offset"] = offset
        out = http_json(base, body)
        result = (out or {}).get("result") or {}
        batch = result.get("points") or []
        points.extend(batch)
        page += 1
        log(f"dump: page {page} — {len(batch)} point(s), {len(points)} total")
        offset = result.get("next_page_offset")
        if offset is None or not batch:
            break
    return points


def cmd_dump(args: argparse.Namespace) -> int:
    collection = args.collection
    # Two exports, two destinations: the payload-only file is the committed,
    # reviewable copy; the vector file is the live flat store. Defaulting them
    # to the same name would overwrite one with the other — which is what the
    # first cut of --with-vectors did.
    out_path = Path(args.out) if args.out else (
        DEFAULT_VECTOR_EXPORT if args.with_vectors else DEFAULT_EXPORT)

    try:
        info = http_json(f"{qdrant_url()}/collections/{collection}",
                         method="GET")
    except RuntimeError as exc:
        die(f"{exc}\n  Is Qdrant up? Start the stack with `aidup`, or "
            f"`docker compose -f services/stacks/mem0/docker-compose.yml up -d`.")
    reported = ((info or {}).get("result") or {}).get("points_count")
    log(f"collection '{collection}': {reported} point(s) reported")

    try:
        points = scroll_all(collection, with_vectors=bool(args.with_vectors))
    except RuntimeError as exc:
        die(str(exc))

    if not points:
        die(f"collection '{collection}' returned no points — refusing to "
            f"write an empty export over {out_path}", 3)

    # Sort by string id so the file has a stable order and git diffs show
    # real changes rather than Qdrant's internal iteration order.
    points.sort(key=lambda p: str(p.get("id")))

    if args.with_vectors:
        # {"id", "vector", "payload"} — the shape memory_flat.load() requires.
        # A point whose vector did not come back is dropped rather than written
        # without one: memory_flat skips vectorless rows, so writing them would
        # produce a store that silently holds fewer points than the file shows.
        records = []
        dropped = 0
        for p in points:
            vector = p.get("vector")
            if isinstance(vector, dict):        # named-vector collections
                vector = next(iter(vector.values()), None)
            if not isinstance(vector, list) or not vector:
                dropped += 1
                continue
            records.append({"id": p.get("id"), "vector": vector,
                            "payload": p.get("payload") or {}})
        if dropped:
            log(f"warn: {dropped} point(s) came back without a vector — omitted")
        if not records:
            die("no point carried a vector — is this collection empty of "
                "embeddings, or did the scroll drop them?", 3)
    else:
        records = [{"id": p.get("id"), "payload": p.get("payload") or {}}
                   for p in points]

    # Secret gate — fail closed. The export is destined for git, so a finding
    # blocks the write rather than warning about it. Scanning happens on the
    # written file, because layer 1 (gitleaks) works on files.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    write_jsonl(tmp, records)

    log(f"secret gate: {gate_layers_note()}")
    flagged = gate(tmp, records)
    if flagged:
        log(f"SECRET GATE: {len(flagged)} of {len(records)} record(s) carry "
            f"credential shapes:")
        for pid, rules in sorted(flagged.items()):
            log(f"  {pid}  [{', '.join(sorted(set(rules)))}]")
        if not args.exclude_flagged:
            tmp.unlink(missing_ok=True)
            die("refusing to write an export containing credentials.\n"
                "  These points are also injected into stage prompts by\n"
                "  memory_inject.py recall, so filtering the file alone does\n"
                "  not make them safe. Preferred fix:\n"
                "    scripts/qdrant-memory.py purge --flagged --yes\n"
                "  then rotate the exposed credentials and re-run dump.\n"
                "  To export the remaining records without purging:\n"
                "    scripts/qdrant-memory.py dump --exclude-flagged", 3)
        before = len(records)
        records = [r for r in records if str(r.get("id")) not in flagged]
        write_jsonl(tmp, records)
        log(f"--exclude-flagged: dropped {before - len(records)} record(s); "
            f"they remain in the collection and are still recallable")
        residual = gate(tmp, records)
        if residual:
            tmp.unlink(missing_ok=True)
            die(f"gate still flags {len(residual)} record(s) after exclusion "
                f"— refusing to write", 3)
    log("secret gate: export is clean")
    tmp.replace(out_path)

    size_kb = out_path.stat().st_size / 1024
    log(f"wrote {len(records)} record(s) to {out_path} ({size_kb:.0f} KB)")
    missing = sum(1 for r in records
                  if not any((r.get("payload") or {}).get(k) for k in TEXT_KEYS))
    if missing:
        log(f"note: {missing} record(s) carry no recognised text key "
            f"{TEXT_KEYS} — they dump fine but will be skipped on restore")
    return 0


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------

def read_jsonl(path: Path) -> "list[dict]":
    if not path.exists():
        die(f"export not found: {path}", 3)
    records = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                die(f"{path}:{lineno}: malformed JSON — {exc}", 3)
    return records


def record_text(record: dict) -> "str | None":
    payload = record.get("payload") or {}
    for key in TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def embed_batch(texts: "list[str]") -> "list[list[float]]":
    out = http_json(f"{tei_url()}/embed", {"inputs": texts}, timeout=120)
    if not isinstance(out, list) or len(out) != len(texts):
        raise RuntimeError(
            f"TEI returned {type(out).__name__} of unexpected shape for "
            f"{len(texts)} input(s)")
    return out


def ensure_collection(collection: str, dim: int) -> None:
    try:
        http_json(f"{qdrant_url()}/collections/{collection}", method="GET")
        return
    except RuntimeError:
        pass
    log(f"collection '{collection}' absent — creating ({dim}-dim, Cosine)")
    http_json(f"{qdrant_url()}/collections/{collection}",
              {"vectors": {"size": dim, "distance": "Cosine"},
               "on_disk_payload": True}, method="PUT")


def existing_ids(collection: str) -> "set[str]":
    ids: set[str] = set()
    base = f"{qdrant_url()}/collections/{collection}/points/scroll"
    offset = None
    while True:
        body: dict = {"limit": SCROLL_PAGE, "with_payload": False,
                      "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        out = http_json(base, body)
        result = (out or {}).get("result") or {}
        batch = result.get("points") or []
        ids.update(str(p.get("id")) for p in batch)
        offset = result.get("next_page_offset")
        if offset is None or not batch:
            break
    return ids


def cmd_restore(args: argparse.Namespace) -> int:
    collection = args.collection
    records = read_jsonl(Path(args.infile))
    log(f"read {len(records)} record(s) from {args.infile}")

    usable, skipped = [], 0
    for record in records:
        if record_text(record) is None:
            skipped += 1
            continue
        usable.append(record)
    if skipped:
        log(f"skipping {skipped} record(s) with no text to embed")
    if not usable:
        die("nothing to restore — no record carried usable text", 3)

    # Probe TEI once, both to fail fast and to learn the live vector width
    # rather than assuming the collection's.
    try:
        probe = embed_batch(["probe"])
    except RuntimeError as exc:
        die(f"{exc}\n  Is the TEI embedding server up on {tei_url()}?")
    dim = len(probe[0])
    log(f"TEI is serving {dim}-dim vectors")

    if args.dry_run:
        log(f"dry run: would embed and upsert {len(usable)} record(s) into "
            f"'{collection}' at {qdrant_url()}")
        return 0

    try:
        ensure_collection(collection, dim)
        present = set() if args.overwrite else existing_ids(collection)
    except RuntimeError as exc:
        die(str(exc))
    if present:
        log(f"collection already holds {len(present)} point(s); "
            f"ids already present are left untouched (use --overwrite to replace)")

    pending = [r for r in usable if args.overwrite or str(r.get("id")) not in present]
    log(f"{len(pending)} record(s) to write")
    if not pending:
        log("nothing new — collection already matches the export.")
        return 0

    written = 0
    buffer: list[dict] = []
    for start in range(0, len(pending), EMBED_BATCH):
        chunk = pending[start:start + EMBED_BATCH]
        texts = [record_text(r) or "" for r in chunk]
        try:
            vectors = embed_batch(texts)
        except RuntimeError as exc:
            die(f"embedding failed at record {start}: {exc}")
        for record, vector in zip(chunk, vectors):
            buffer.append({"id": record["id"],
                           "vector": vector,
                           "payload": record.get("payload") or {}})
        if len(buffer) >= UPSERT_BATCH:
            try:
                http_json(f"{qdrant_url()}/collections/{collection}/points?wait=true",
                          {"points": buffer}, method="PUT")
            except RuntimeError as exc:
                die(f"upsert failed after {written} record(s): {exc}")
            written += len(buffer)
            log(f"restore: {written}/{len(pending)} written")
            buffer = []
    if buffer:
        try:
            http_json(f"{qdrant_url()}/collections/{collection}/points?wait=true",
                      {"points": buffer}, method="PUT")
        except RuntimeError as exc:
            die(f"upsert failed after {written} record(s): {exc}")
        written += len(buffer)

    log(f"restored {written} record(s) into '{collection}'")
    return 0


# ---------------------------------------------------------------------------
# purge
# ---------------------------------------------------------------------------

def cmd_purge(args: argparse.Namespace) -> int:
    """Delete credential-carrying points from the live collection.

    Filtering the export is not enough: `recall()` reads the collection, not
    the file, so a flagged point keeps being eligible for injection into stage
    prompts until it is actually removed."""
    collection = args.collection
    try:
        points = scroll_all(collection)
    except RuntimeError as exc:
        die(str(exc))

    records = [{"id": p.get("id"), "payload": p.get("payload") or {}}
               for p in points]
    log(f"secret gate: {gate_layers_note()}")
    with tempfile.TemporaryDirectory() as tmpdir:
        scratch = Path(tmpdir) / "scan.jsonl"
        write_jsonl(scratch, records)
        flagged = gate(scratch, records)
    if not flagged:
        log(f"nothing to purge — {len(records)} record(s) scanned, all clean.")
        return 0

    log(f"{len(flagged)} of {len(records)} record(s) carry credential shapes:")
    for pid, rules in sorted(flagged.items()):
        log(f"  {pid}  [{', '.join(sorted(set(rules)))}]")

    if not args.yes:
        log("dry run — nothing deleted. Re-run with --yes to purge.")
        log("NOTE: purging removes the point but does NOT rotate the exposed "
            "credential. Rotate it too.")
        return 0

    ids = [r["id"] for r in records if str(r.get("id")) in flagged]
    try:
        http_json(f"{qdrant_url()}/collections/{collection}/points/delete?wait=true",
                  {"points": ids}, method="POST")
    except RuntimeError as exc:
        die(f"delete failed: {exc}")

    after = [{"id": p.get("id"), "payload": p.get("payload") or {}}
             for p in scroll_all(collection)]
    with tempfile.TemporaryDirectory() as tmpdir:
        scratch = Path(tmpdir) / "verify.jsonl"
        write_jsonl(scratch, after)
        remaining = gate(scratch, after)
    if remaining:
        die(f"purge incomplete — {len(remaining)} flagged record(s) remain", 3)
    log(f"purged {len(ids)} record(s); collection re-scanned clean.")
    log("Now rotate the exposed credentials — deletion does not un-expose them.")
    return 0


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

def cmd_stats(args: argparse.Namespace) -> int:
    path = Path(args.infile)
    if path.exists():
        records = read_jsonl(path)
        kinds: dict = {}
        repos: dict = {}
        for record in records:
            payload = record.get("payload") or {}
            kinds[payload.get("kind") or "(untyped/legacy)"] = \
                kinds.get(payload.get("kind") or "(untyped/legacy)", 0) + 1
            if payload.get("target_repo"):
                repos[payload["target_repo"]] = repos.get(payload["target_repo"], 0) + 1
        size_kb = path.stat().st_size / 1024
        log(f"export {path} — {len(records)} record(s), {size_kb:.0f} KB")
        for kind, count in sorted(kinds.items(), key=lambda kv: -kv[1]):
            log(f"  kind: {kind:<24} {count}")
        for repo, count in sorted(repos.items(), key=lambda kv: -kv[1]):
            log(f"  target_repo: {repo:<24} {count}")
    else:
        log(f"export {path} — absent")

    try:
        info = http_json(f"{qdrant_url()}/collections/{args.collection}",
                         method="GET")
        result = (info or {}).get("result") or {}
        log(f"qdrant '{args.collection}' — {result.get('points_count')} point(s), "
            f"status={result.get('status')}")
    except RuntimeError as exc:
        log(f"qdrant — unreachable ({exc})")
    return 0


def _ephemeral_predicate():
    """The one definition of "ephemeral target", imported, never re-implemented.

    `dispatcher/memory_inject._is_ephemeral_target` is what write-back has
    refused since T02, so a purge built on anything else would delete a
    different set than the guard prevents — two rules drifting apart is exactly
    what put this garbage in the store to begin with.
    """
    here = Path(__file__).resolve().parent.parent / "dispatcher"
    sys.path.insert(0, str(here))
    from memory_inject import _is_ephemeral_target   # noqa: E402
    return _is_ephemeral_target


def cmd_purge_ephemeral(args: argparse.Namespace) -> int:
    """Drop points whose target_repo was a throwaway temp directory.

    These predate the T02 write-back guard (2026-08-20): runner-level tests
    that reached pipeline completion while a store was listening wrote a real
    `task_lesson` for a `$TMPDIR` fixture. No new ones appear, but the old ones
    dilute every recall — the scoped half of `recall()` filters by
    `target_repo`, and these are targets that no longer exist.

    Selection is by `target_repo` ONLY, deliberately. Widening it to "any temp
    path mentioned anywhere in the record" also catches real session summaries
    that merely quote a path in passing — six of them in the 2026-08-28 export,
    including operator hand-off notes. Those are content, not residue.
    """
    is_ephemeral = _ephemeral_predicate()
    targets = [Path(p) for p in (args.files or
                                 [DEFAULT_EXPORT, DEFAULT_VECTOR_EXPORT])]

    total_removed = 0
    for path in targets:
        if not path.exists():
            log(f"{path.name}: not present, skipped "
                f"(the vectors file is gitignored and lives on the host)")
            continue
        records = read_jsonl(path)
        keep, drop = [], []
        for rec in records:
            payload = rec.get("payload") or rec
            (drop if is_ephemeral(payload.get("target_repo") or "") else
             keep).append(rec)

        log(f"{path.name}: {len(records)} record(s), {len(drop)} ephemeral")
        for rec in drop:
            payload = rec.get("payload") or rec
            text = " ".join((payload.get("text") or "").split())[:80]
            log(f"    {str(rec.get('id'))[:8]}  {text}")

        if not drop:
            continue
        if not args.yes:
            log(f"    dry run — pass --yes to rewrite {path.name}")
            continue
        write_jsonl(path, keep)
        log(f"    rewrote {path.name}: {len(records)} -> {len(keep)}")
        total_removed += len(drop)

    if args.yes and total_removed:
        log(f"removed {total_removed} record(s) in total.")
        log("The live flat store is a separate file on the host "
            "(MEMORY_FLAT_PATH); run this there too, or recall keeps serving them.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="qdrant-memory.py",
        description="Export/import the pipeline's semantic memory as JSONL.")
    parser.add_argument("--collection",
                        default=env("MEMORY_COLLECTION", "meta_agent_mem"),
                        help="Qdrant collection (default: %(default)s)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_dump = sub.add_parser("dump", help="Qdrant -> JSONL")
    p_dump.add_argument("--out", default=None,
                        help=f"default: {DEFAULT_EXPORT} (payload-only) or "
                             f"{DEFAULT_VECTOR_EXPORT} with --with-vectors")
    p_dump.add_argument("--exclude-flagged", action="store_true",
                        help="write the export without records that trip the "
                             "secret gate, instead of refusing outright")
    p_dump.add_argument("--with-vectors", action="store_true",
                        help="include the embedding vectors. The default export "
                             "is payload-only, so it can only be restored by "
                             "RE-EMBEDDING every text through TEI — and it "
                             "cannot serve as the flat store at all. Pass this "
                             "to produce the file MEMORY_FLAT_PATH points at "
                             "(T13); expect ~3 MB per 800 points.")
    p_dump.set_defaults(func=cmd_dump)

    p_purge = sub.add_parser(
        "purge", help="delete credential-carrying points from the collection")
    p_purge.add_argument("--flagged", action="store_true",
                         help="select by the secret gate (currently the only "
                              "selector; present for explicitness)")
    p_purge.add_argument("--yes", action="store_true",
                         help="actually delete; without it this is a dry run")
    p_purge.set_defaults(func=cmd_purge)

    p_pe = sub.add_parser(
        "purge-ephemeral",
        help="drop points whose target_repo was a throwaway temp directory")
    p_pe.add_argument("files", nargs="*",
                      help=f"JSONL files to clean (default: {DEFAULT_EXPORT.name} "
                           f"and {DEFAULT_VECTOR_EXPORT.name})")
    p_pe.add_argument("--yes", action="store_true",
                      help="actually rewrite the files; without it this is a dry run")
    p_pe.set_defaults(func=cmd_purge_ephemeral)

    p_restore = sub.add_parser("restore", help="JSONL -> Qdrant (re-embeds)")
    p_restore.add_argument("--in", dest="infile", default=str(DEFAULT_EXPORT))
    p_restore.add_argument("--dry-run", action="store_true")
    p_restore.add_argument("--overwrite", action="store_true",
                           help="rewrite points whose id already exists")
    p_restore.set_defaults(func=cmd_restore)

    p_stats = sub.add_parser("stats", help="describe export and/or collection")
    p_stats.add_argument("--in", dest="infile", default=str(DEFAULT_EXPORT))
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
