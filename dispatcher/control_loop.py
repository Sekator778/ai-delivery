"""Control-loop primitives for the agent pipeline.

The pure, side-effect-light core of the pipeline governor: convergence
detection (anti-thrash trend gate), claude stream-json cost parsing,
per-stage token/cost reads, and the atomic token accumulator. Depends only
on the standard library and STAGE_ARTIFACT_MAP — no telegram, no triage
policy, no state mutation — so it imports cleanly with no cycle and is the
language-agnostic slice worth extracting as a library (PLAN.md, ход 2/B).
stage_runner_agent re-imports every name, so its public surface and the
test suite are unchanged.
"""

import fcntl
import json
import re
from pathlib import Path

from stage_prompts import STAGE_ARTIFACT_MAP


def _read_stage_cost_usd(task_dir: Path, stage: str) -> float:
    """Read total_cost_usd from a stage's JSON artifact. Returns 0.0 if the
    JSON is missing or doesn't carry cost info (treat as zero-cost — the
    cost cap is a safety guard, not a billing system)."""
    json_path = task_dir / STAGE_ARTIFACT_MAP[stage].replace(".md", ".json")
    if not json_path.exists():
        return 0.0
    try:
        data = json.loads(json_path.read_text())
        return float(data.get("cost", {}).get("total_cost_usd") or 0.0)
    except Exception:
        return 0.0


def _read_stage_tokens(task_dir: Path, stage: str) -> int:
    """Read input+output tokens from a stage's JSON artifact. Returns 0 if the
    JSON is missing or carries no usage. Tokens are the subscription-relevant
    budget unit (the $ figure is notional under a flat plan); the triage layer
    sizes and caps in tokens — see dispatcher/triage.py."""
    json_path = task_dir / STAGE_ARTIFACT_MAP[stage].replace(".md", ".json")
    if not json_path.exists():
        return 0
    try:
        cost = (json.loads(json_path.read_text()).get("cost") or {})
        return int(cost.get("input_tokens") or 0) + int(cost.get("output_tokens") or 0)
    except Exception:
        return 0


def _parse_cost(stream_json_output: str) -> dict:
    """Pull total_cost_usd / total_input_tokens / total_output_tokens from
    the claude stream-json output's final `result` event. Returns {} if
    no result event found (e.g. transport error)."""
    for line in reversed(stream_json_output.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            usage = event.get("usage", {})
            return {
                "total_cost_usd": event.get("total_cost_usd"),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                # Cache tokens carry most of a stage's real input volume
                # (input_tokens excludes cache reads): a live tester stage
                # showed input_tokens=14 against output=5189 with the bulk in
                # cache reads. Without these, a provider-priced recompute
                # (backend_routing.apply_backend_pricing) undercounts.
                "cache_read_tokens": usage.get("cache_read_input_tokens"),
                "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
                "session_id": event.get("session_id"),
            }
    return {}


def _analyze_critical_count(task_dir: Path) -> int | None:
    """WS-5 — parse the TRAILING CRITICAL_COUNT line from the analyze report
    (02c-analyze.md). Returns the integer count, or None if the report or the
    marker is absent. Anchors on the LAST line-matching occurrence (the contract
    requires CRITICAL_COUNT to be the final line) so a prose/table mention of the
    token earlier in the report cannot false-block — same shape-not-first-match
    rule as the PR-URL extractor."""
    for name in ("02c-analyze.md", "02c-analyze-agent.md"):
        p = task_dir / name
        if p.is_file():
            matches = re.findall(
                r"^CRITICAL_COUNT\s*:\s*(\d+)\s*$",
                p.read_text(encoding="utf-8", errors="replace"),
                re.IGNORECASE | re.MULTILINE,
            )
            if matches:
                return int(matches[-1])
    return None


def _reviewer_critical_count(task_dir: Path) -> int:
    """Critical-finding count from the latest reviewer verdict
    (06-review-agent.json / 06-review.json). 0 when absent/unparseable. Used to
    gate the upgrade ladder: a 0-critical (pure nitpick) request_changes must
    NOT escalate an S task — escalating would re-open the very hotfix loop the
    S cap exists to prevent."""
    for name in ("06-review-agent.json", "06-review.json"):
        p = task_dir / name
        if p.is_file():
            try:
                v = (json.loads(p.read_text()).get("verdict") or {})
                return int(v.get("critical") or 0)
            except Exception:
                return 0
    return 0


def _critical_is_converging(trend, k) -> bool:
    """True iff the reviewer's critical-finding count is on a STRICTLY
    DECREASING run over the last ``k+1`` recorded passes — i.e. the hotfix loop
    is making genuine progress toward zero ([5,3,1]). False for a flat or rising
    tail ([2,2] — the M-validation incident, or [2,3,2] — thrash), which means
    the loop is burning iterations without converging.

    Gates the upgrade ladder: a converging loop earns more rope (the M→L bump),
    a thrashing one is cut at the cap and handed to the operator. Fail-open — an
    empty / single-entry trend counts as converging, so a missing trend never
    blocks the FIRST upgrade (preserves today's behaviour before any thrash is
    observable). ``k`` is STAGNANT_STREAK_CAP (default 2 → a 3-pass window)."""
    w = max(2, int(k) + 1)
    tail = [int(x) for x in (trend or [])][-w:]
    if len(tail) < 2:
        return True
    return all(tail[i] > tail[i + 1] for i in range(len(tail) - 1))


def _add_tokens_used(task_dir: Path, delta: int) -> int:
    """Atomically add `delta` to state.tokens_used; return the new total. A
    single flock'd read-modify-write so concurrent stages (tester‖security)
    never lose an increment."""
    sp = task_dir / "state.json"
    if not sp.exists():
        return int(delta)
    with sp.open("r+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        data = json.loads(f.read())
        total = int(data.get("tokens_used") or 0) + int(delta)
        data["tokens_used"] = total
        f.seek(0)
        f.truncate()
        f.write(json.dumps(data, indent=2) + "\n")
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return total
