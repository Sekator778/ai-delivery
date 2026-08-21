"""Task-scoped memory for pipeline stages — recall + typed write-back.

Roadmap priority #0 (owner, 2026-08-14): before the thinking/building stages
run, retrieve memory relevant to THIS target repo and THIS request and put it
in front of the subagent; at task end, write back a TYPED record instead of a
prose dump, because typed metadata is what makes the scoped retrieval possible.

Why this lives in the runner and not in Claude Code hooks: the old
`inject_from_mem0` UserPromptSubmit hook ran under the system python3 (no
`fastembed`), swallowed its own failure (`exit 0`), and after the stage-cwd
move (2026-08-15) stage sessions no longer start in this repo at all, so this
repo's settings hooks never fire for them. The runner, by contrast, builds
every stage prompt anyway — `fill_slot` replaces the literal `(none)` between
the `<injected-memory>` markers that the ba/architect/developer prompts
already carry, deterministically, before the subprocess is spawned.

Infrastructure (both long-running local services, checked per call with short
timeouts): TEI embedding server (BAAI/bge-m3, 1024-dim) and Qdrant. The
existing `meta_agent_mem` collection (bge-m3 vectors) is reused: its legacy
points are unscoped prose from interactive sessions — still useful as global
semantic hints — while write-back adds scoped points carrying
`{kind: "task_lesson", target_repo, ...}` payloads that the filtered half of
the recall can target precisely.

Failure contract: EVERY public function degrades to a no-op — a stage must
never fail, stall, or change behavior because memory infra is down. Timeouts
are short; errors are logged to stderr and swallowed.

Env knobs (read at call time, so tests and operators can flip them live):
  MEMORY_INJECT_ENABLED     default 1
  MEMORY_WRITEBACK_ENABLED  default 1 (write-back additionally refuses any
                            target_repo under the system temp dir — see
                            _is_ephemeral_target)
  MEMORY_INJECT_STAGES      default "ba,architect,developer"
  MEMORY_TOP_K              default 5   (total, scoped hits first)
  MEMORY_MIN_SCORE          default 0.4 (cosine floor — below is noise)
  MEMORY_TARGET_CAP         default 200 (task_lesson points kept per target)
  MEMORY_TEI_URL            default http://127.0.0.1:8087
  MEMORY_QDRANT_URL         default http://127.0.0.1:6333
  MEMORY_COLLECTION         default meta_agent_mem
  MEMORY_HTTP_TIMEOUT       default 3 (seconds, per HTTP call)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.request
import uuid
from datetime import datetime, timezone

# The literal slot the stage prompts carry. fill_slot replaces it wholesale,
# so the marker text here and in stage_prompts.py must match byte-for-byte —
# tests/test_memory_inject.py pins both sides.
SLOT = "<injected-memory>\n(none)\n</injected-memory>"

_ENTRY_CHAR_CAP = 700     # per recalled record
_BLOCK_CHAR_CAP = 4000    # whole injected block — protect the context budget


def _env(name: str, default: str) -> str:
    return (os.environ.get(name) or "").strip() or default


def _is_ephemeral_target(target_repo: str) -> bool:
    """True for a target repo that lives under the system temp directory.

    Write-back refuses these (backlog/T02, 2026-08-20). A dump of the live
    `meta_agent_mem` collection held 22 typed `task_lesson` points; **21** of
    them carried a `target_repo` under macOS `$TMPDIR` —
    `/var/folders/.../T/tmpXXXX/repo`, the throwaway fixtures of test runs that
    reached pipeline completion while a Qdrant happened to be listening on the
    default URL. Nobody noticed, because the module's failure contract is to
    degrade silently.

    The cost is not noise. `recall()` runs two searches, one of them **filtered
    by `target_repo`** — that scoped half is the whole point of roadmap #0, and
    it was backed almost entirely by directories that no longer exist. Nor does
    `_retire_over_cap` help: it keeps `MEMORY_TARGET_CAP` points *per target*,
    so junk targets never evict each other, they each get their own budget
    forever.

    This is a deliberate production behavior change, not only a test guard: a
    lesson scoped to a directory that is deleted when the process exits can
    never be recalled by anything, so writing it is pure dilution. Worktrees
    are unaffected — write-back is handed `spec.json`'s `target_repo` (the real
    project path), never the `/tmp/ai-delivery-wt/...` checkout a stage runs
    in. An operator who genuinely targets a repo under /tmp loses memory for
    that run, and only that run.
    """
    if not target_repo:
        return False
    try:
        resolved = os.path.realpath(target_repo)
    except (OSError, ValueError):
        return False
    for root in {tempfile.gettempdir(), "/tmp"}:
        try:
            root_resolved = os.path.realpath(root)
        except (OSError, ValueError):
            continue
        if resolved == root_resolved or resolved.startswith(root_resolved.rstrip("/") + os.sep):
            return True
    return False


def _enabled(flag: str) -> bool:
    return _env(flag, "1").lower() not in ("0", "false", "no", "off")


def _timeout() -> float:
    try:
        return float(_env("MEMORY_HTTP_TIMEOUT", "3"))
    except ValueError:
        return 3.0


def _post_json(url: str, payload: dict,
               method: str = "POST") -> "dict | list | None":
    """Send JSON, parse JSON. None on ANY failure — this module never raises.
    Qdrant's API is method-sensitive: search/scroll/delete are POST, the
    points upsert is PUT (POST there is a 400 — caught by the live canary)."""
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method=method)
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 — degrade, never break a stage
        print(f"[memory-inject] warn: {url.split('/collections')[0]}: {exc}",
              file=sys.stderr)
        return None


def _embed(text: str) -> "list[float] | None":
    out = _post_json(_env("MEMORY_TEI_URL", "http://127.0.0.1:8087") + "/embed",
                     {"inputs": [text[:6000]]})
    if isinstance(out, list) and out and isinstance(out[0], list):
        return out[0]
    return None


def _search(vector: "list[float]", limit: int,
            target_repo: "str | None" = None) -> "list[dict]":
    body: dict = {"vector": vector, "limit": limit, "with_payload": True}
    try:
        body["score_threshold"] = float(_env("MEMORY_MIN_SCORE", "0.4"))
    except ValueError:
        body["score_threshold"] = 0.4
    if target_repo:
        body["filter"] = {"must": [
            {"key": "target_repo", "match": {"value": target_repo}}]}
    out = _post_json(
        f"{_env('MEMORY_QDRANT_URL', 'http://127.0.0.1:6333')}/collections/"
        f"{_env('MEMORY_COLLECTION', 'meta_agent_mem')}/points/search", body)
    if isinstance(out, dict) and isinstance(out.get("result"), list):
        return out["result"]
    return []


def recall(query: str, target_repo: str) -> "list[dict]":
    """Top-K memory hits for this request: points scoped to the target repo
    first (typed write-back records), then global semantic matches (legacy
    prose points have no target_repo payload, so only the unfiltered search
    can surface them). Deduplicated by point id."""
    vector = _embed(query)
    if not vector:
        return []
    try:
        top_k = int(_env("MEMORY_TOP_K", "5"))
    except ValueError:
        top_k = 5
    hits: list[dict] = []
    seen: set = set()
    for batch in (_search(vector, top_k, target_repo),
                  _search(vector, top_k)):
        for hit in batch:
            if hit.get("id") not in seen:
                seen.add(hit.get("id"))
                hits.append(hit)
    return hits[:top_k]


def format_block(hits: "list[dict]") -> str:
    lines = ["[recalled memory — non-authoritative hints from past sessions]"]
    for i, hit in enumerate(hits, 1):
        p = hit.get("payload") or {}
        stamp = (p.get("timestamp") or p.get("ts") or "")[:10]
        origin = p.get("kind") or p.get("source") or "memory"
        text = " ".join((p.get("text") or "").split())[:_ENTRY_CHAR_CAP]
        if not text:
            continue
        lines.append(f"{i}. ({stamp} {origin}) {text}")
    block = "\n".join(lines)
    return block[:_BLOCK_CHAR_CAP]


def fill_slot(prompt: str, *, stage: str, query: str, target_repo: str) -> str:
    """Replace the prompt's `(none)` memory slot with recalled records.

    Unchanged prompt on every miss: inject disabled, stage not opted in, no
    slot marker in this prompt, memory infra down, or nothing relevant found.
    """
    if not _enabled("MEMORY_INJECT_ENABLED"):
        return prompt
    stages = {s.strip() for s in
              _env("MEMORY_INJECT_STAGES", "ba,architect,developer").split(",")}
    if stage not in stages or SLOT not in prompt:
        return prompt
    hits = recall(query, target_repo)
    if not hits:
        print(f"[memory-inject] {stage}: no records (infra down or nothing "
              f"relevant)", file=sys.stderr)
        return prompt
    block = format_block(hits)
    print(f"[memory-inject] {stage}: {len(hits)} record(s), "
          f"{len(block)} chars", file=sys.stderr)
    return prompt.replace(
        SLOT, f"<injected-memory>\n{block}\n</injected-memory>", 1)


# ── Write-back ─────────────────────────────────────────────────────────────

def write_back(*, task_id: str, target_repo: str, spec_prompt: str,
               state: dict, stop_reason: str) -> bool:
    """Append one typed task_lesson point at pipeline completion.

    Deterministic composition (no extra LLM call): the typed FIELDS are the
    point — kind/target_repo/tier/verdict — and the text is a compact factual
    summary a later recall can rank. Returns False (silently, logged) on any
    infra failure; the pipeline outcome is never affected.
    """
    if not _enabled("MEMORY_WRITEBACK_ENABLED"):
        return False
    if _is_ephemeral_target(target_repo):
        print(f"[memory-inject] write-back skipped: target {target_repo} is "
              f"under the system temp dir (ephemeral — see _is_ephemeral_target)",
              file=sys.stderr)
        return False
    tier = (state.get("triage") or {}).get("tier") or state.get("tier") or "-"
    text = (
        f"Task {task_id} on {target_repo} finished "
        f"(stop_reason={stop_reason}, tier={tier}, "
        f"iteration={state.get('iteration')}, "
        f"cost=${float(state.get('cost_usd') or 0):.2f}, "
        f"pr={state.get('pr_url') or '-'}). Request: "
        + " ".join((spec_prompt or "").split())[:400]
    )
    vector = _embed(text)
    if not vector:
        return False
    point = {
        "id": str(uuid.uuid4()),
        "vector": vector,
        "payload": {
            "kind": "task_lesson",
            "source": "pipeline_writeback",
            "target_repo": target_repo,
            "task_id": task_id,
            "tier": tier,
            "stop_reason": stop_reason,
            "pr_url": state.get("pr_url") or "",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "text": text,
        },
    }
    out = _post_json(
        f"{_env('MEMORY_QDRANT_URL', 'http://127.0.0.1:6333')}/collections/"
        f"{_env('MEMORY_COLLECTION', 'meta_agent_mem')}/points?wait=true",
        {"points": [point]}, method="PUT")
    if not (isinstance(out, dict) and out.get("status") == "ok"):
        return False
    _retire_over_cap(target_repo)
    return True


def _retire_over_cap(target_repo: str) -> None:
    """Dilution guard: keep at most MEMORY_TARGET_CAP task_lesson points per
    target; delete the oldest beyond it. Ungated accumulation measurably
    regresses retrieval (drift papers, roadmap Do NOW #1) — cap + retire."""
    try:
        cap = int(_env("MEMORY_TARGET_CAP", "200"))
    except ValueError:
        cap = 200
    base = (f"{_env('MEMORY_QDRANT_URL', 'http://127.0.0.1:6333')}/collections/"
            f"{_env('MEMORY_COLLECTION', 'meta_agent_mem')}/points")
    flt = {"must": [
        {"key": "kind", "match": {"value": "task_lesson"}},
        {"key": "target_repo", "match": {"value": target_repo}}]}
    out = _post_json(base + "/scroll",
                     {"filter": flt, "limit": max(cap * 2, cap + 50),
                      "with_payload": ["timestamp"], "with_vector": False})
    points = (out or {}).get("result", {}).get("points", []) \
        if isinstance(out, dict) else []
    if len(points) <= cap:
        return
    points.sort(key=lambda p: (p.get("payload") or {}).get("timestamp") or "")
    stale = [p["id"] for p in points[: len(points) - cap]]
    _post_json(base + "/delete?wait=true", {"points": stale})
    print(f"[memory-inject] retired {len(stale)} stale task_lesson point(s) "
          f"for {target_repo}", file=sys.stderr)
