"""Flat-file semantic store — the successor to Qdrant for this collection
(backlog/T13 verdict, ROADMAP "Phase Memory-Footprint").

The measurement that decided it: 600 MB on disk and two always-on services for
809 points, of which 787 are frozen prose from hooks retired in `a364eb6` and
exactly ONE typed lesson carries a real target. The vectors themselves are
3.16 MB as float32, and a stdlib top-5 cosine scan over the whole collection
takes ~70 ms here — against three recalls per task and stages measured in
minutes, that is noise. (The earlier ROADMAP wording said "microseconds"; it
was wrong, and 70 ms is the honest number.)

So: same recall, same payloads, one JSONL file, no database. TEI is still
required — the query has to be embedded — which is why this is the flat-store
verdict and not the drop-semantics one.

Shape, deliberately identical to what `scripts/qdrant-memory.py dump
--with-vectors` writes, so the migration is "run the dump, point the env at
it"::

    {"id": "...", "vector": [0.01, ...], "payload": {"text": "...", ...}}

OFF by default. ``MEMORY_FLAT_ENABLED=1`` switches recall and write-back over;
until then every path in memory_inject goes to Qdrant exactly as before. When
the flag is on and the file is missing the module says so loudly and degrades
to no hits rather than silently falling back — a store that is quietly not
there is the T01 failure shape (memory dead, pipeline reporting healthy).
"""
from __future__ import annotations

import json
import math
import os
import sys
import threading
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_PATH = _REPO_ROOT / "memory-bank" / "semantic-export" / "meta_agent_mem.vectors.jsonl"

PATH_ENV = "MEMORY_FLAT_PATH"
FLAG_ENV = "MEMORY_FLAT_ENABLED"

# (path, mtime, size) -> parsed rows. The runner recalls once per stage in the
# same process; re-reading and re-normalising a few megabytes each time is
# pointless, and the key makes a stale cache impossible after a write.
_CACHE: "dict[tuple, list[dict]]" = {}
_LOCK = threading.Lock()


def enabled() -> bool:
    return (os.environ.get(FLAG_ENV) or "").strip() == "1"


def store_path() -> Path:
    override = (os.environ.get(PATH_ENV) or "").strip()
    return Path(override).expanduser() if override else _DEFAULT_PATH


def _norm(vector: "list[float]") -> float:
    return math.sqrt(sum(x * x for x in vector)) or 1.0


def load() -> "list[dict]":
    """Rows with a usable vector, normalised once. Empty (loudly) on a missing
    or unreadable store — this module never raises into a stage."""
    path = store_path()
    try:
        stat = path.stat()
    except OSError:
        print(f"[memory-flat] warn: store not found at {path} — recall is a "
              f"no-op until `scripts/qdrant-memory.py dump --with-vectors` "
              f"has written it", file=sys.stderr)
        return []
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
    rows: list[dict] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue          # one bad line must not lose the store
                vector = row.get("vector")
                if not isinstance(vector, list) or not vector:
                    continue          # payload-only export: nothing to rank on
                inv = 1.0 / _norm(vector)
                rows.append({
                    "id": row.get("id"),
                    "payload": row.get("payload") or {},
                    "unit": [x * inv for x in vector],
                })
    except OSError as exc:
        print(f"[memory-flat] warn: cannot read {path}: {exc}", file=sys.stderr)
        return []
    with _LOCK:
        _CACHE.clear()               # only the current file is worth caching
        _CACHE[key] = rows
    return rows


def search(vector: "list[float]", limit: int, target_repo: "str | None" = None,
           min_score: float = 0.4) -> "list[dict]":
    """Top-`limit` by cosine, in the hit shape memory_inject already consumes.

    ``target_repo`` is the scoped half of recall — a field comparison here,
    where Qdrant used a payload filter. Same semantics, ten lines."""
    rows = load()
    if not rows or not vector:
        return []
    inv = 1.0 / _norm(vector)
    query = [x * inv for x in vector]
    scored: list[tuple[float, dict]] = []
    for row in rows:
        if target_repo and (row["payload"].get("target_repo") != target_repo):
            continue
        unit = row["unit"]
        if len(unit) != len(query):
            continue                  # a differently-dimensioned leftover row
        score = sum(a * b for a, b in zip(unit, query))
        if score >= min_score:
            scored.append((score, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [{"id": row["id"], "score": score, "payload": row["payload"]}
            for score, row in scored[:limit]]


def append(point: dict) -> bool:
    """Add one point. Append-only: the file is the store, not a cache of one."""
    path = store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(point, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[memory-flat] warn: append to {path} failed: {exc}",
              file=sys.stderr)
        return False
    return True


def retire_over_cap(target_repo: str, cap: int) -> int:
    """Keep at most `cap` task_lesson points for this target, oldest dropped.

    Same dilution guard the Qdrant path has (ungated accumulation measurably
    regresses retrieval). Rewrites the file through a temp file so a crash
    mid-write cannot truncate the store."""
    path = store_path()
    try:
        raw = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    except (OSError, ValueError) as exc:
        print(f"[memory-flat] warn: retire skipped ({exc})", file=sys.stderr)
        return 0

    def _is_target_lesson(row: dict) -> bool:
        payload = row.get("payload") or {}
        return (payload.get("kind") == "task_lesson"
                and payload.get("target_repo") == target_repo)

    lessons = [r for r in raw if _is_target_lesson(r)]
    if len(lessons) <= cap:
        return 0
    lessons.sort(key=lambda r: (r.get("payload") or {}).get("timestamp") or "")
    stale = {id(r) for r in lessons[: len(lessons) - cap]}
    kept = [r for r in raw if id(r) not in stale]
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            for row in kept:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[memory-flat] warn: retire rewrite failed: {exc}", file=sys.stderr)
        tmp.unlink(missing_ok=True)
        return 0
    return len(stale)
