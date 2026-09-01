"""Room driver — the loop T25 shipped without.

T25 built the conductor's brain and no driver. The live run on 2026-09-01 is
the whole diagnosis: the conductor was handed turn one, returned a valid
delegation with a good brief for $0.178, and then nothing happened. No
specialist was spawned, no turn two was built, nothing reached the chat. Not a
flaky failure — `room_command` ended at `await run_subtask(...)` and the
generic completion callback did not know room task ids, so the loop could not
continue by construction.

This module is that loop, and it is deliberately not in bot.py. bot.py cannot
be imported in a test — telegram, aiohttp and a module-level config load — and
the acceptance asks for an offline test against a mocked spawn. So the driver
takes its two effects as callbacks:

    spawn(task_id, cwd, prompt, backend) -> SpawnResult
    notify(text, files=[...])            -> awaited, delivers to the chat

Everything else — turns, budget, history, caps, refusals — is decided here and
is testable with neither a network nor a bot.

Three things this gets right that are easy to get wrong:

  - **Budget is debited in the provider's currency.** The CLI's own cost figure
    is the provider's figure only on Anthropic; on DeepSeek it is not, and
    backend_routing.apply_backend_pricing exists for exactly that. A room that
    trusted the raw number would measure a DeepSeek spend in Anthropic dollars.
  - **The conductor's own turns are debited too.** Turn one of the live run
    cost $0.178 — nine percent of the default cap before any specialist ran. A
    room with a talkative conductor would otherwise burn the cap off the books.
  - **Silence is never an outcome.** Every terminal path — budget exhausted,
    turns exhausted, an unparseable conductor, a specialist that failed —
    sends something. The failure this module exists to fix was silence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import room_conductor as room

try:  # pragma: no cover - the ledger is best-effort and optional in tests
    import backend_routing as _routing
except Exception:  # pragma: no cover
    _routing = None

ROOM_TASK_PREFIX = "room-"

CONDUCTOR_BACKEND = "deepseek"
CONDUCTOR_STAGE = "room-conductor"
SPECIALIST_STAGE = "room-specialist"

# One reprompt on a malformed conductor reply, then an honest refusal. Not
# unlimited: a conductor that cannot produce JSON twice in a row will not
# produce it on the fifth try either, and each attempt costs money.
MAX_PARSE_RETRIES = 1


@dataclass
class SpawnResult:
    rc: int
    output: str
    cost_usd: float = 0.0
    cost_source: str = "cli"


@dataclass
class RoomOutcome:
    status: str                 # 'finished' | 'budget' | 'turns' | 'unparseable'
    summary: str = ""
    spent_usd: float = 0.0
    delegations: int = 0
    files: "list[str]" = field(default_factory=list)


Spawn = Callable[..., Awaitable[SpawnResult]]
Notify = Callable[..., Awaitable[None]]


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------
_RESULT_COST_RE = re.compile(r'"total_cost_usd"\s*:\s*([0-9.]+)')


def extract_result_event(output: str) -> "dict | None":
    """The `{"type":"result", ...}` line of a stream-json run, or None.

    run_subtask hands back the child's whole stdout: hook and system events,
    assistant messages, then the result event. That matters more than it
    sounds. T28 fed the raw stream straight to the decision parser, which looks
    for the first balanced {...} and therefore found
    `{"type":"system","subtype":"hook_started"}` — no `action` field, so every
    turn came back "unparseable", including the perfectly good delegation the
    conductor produced on the live run.

    The model's own final text is the `result` field of this event; the tokens
    are in its `usage`. Scanned from the end because the result event is last
    and a long run has thousands of lines before it.
    """
    if not output:
        return None
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{") or '"result"' not in line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("type") == "result":
            return parsed
    return None


def decision_text(output: str) -> str:
    """What to hand the decision parser.

    The result event's `result` field when there is one; otherwise the raw
    output, which keeps the honest failure path from T28 intact — a child that
    crashed before emitting a result event has no decision to extract, and
    saying so is better than inventing one.
    """
    event = extract_result_event(output)
    if event is None:
        return output
    text = event.get("result")
    return text if isinstance(text, str) and text.strip() else output


# Token names differ between the CLI's result event and the ledger's pricing
# helper, and the cache columns are not optional decoration: cache writes are
# billed at full input rates and cache reads at a fraction, so dropping either
# skews the total in a direction nobody would notice.
_USAGE_KEY_MAP = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_input_tokens": "cache_read_tokens",
    "cache_creation_input_tokens": "cache_creation_tokens",
}


def _flatten_usage(event: dict) -> dict:
    """Lift `usage` onto the top level under the names apply_backend_pricing reads."""
    flat = {"total_cost_usd": event.get("total_cost_usd")}
    usage = event.get("usage")
    if isinstance(usage, dict):
        for src, dst in _USAGE_KEY_MAP.items():
            if src in usage:
                flat[dst] = usage[src]
    # A flat event (older CLI, or a hand-built fixture) already uses these names.
    for key in ("input_tokens", "output_tokens",
                "cache_read_tokens", "cache_creation_tokens"):
        if key in event and key not in flat:
            flat[key] = event[key]
    return flat


def parse_cost(output: str, backend: str) -> "tuple[float, str]":
    """Cost of one child, in the provider's own money.

    Reads the stream-json `result` line, then runs it through
    backend_routing.apply_backend_pricing — which recomputes from tokens for
    non-Anthropic backends, because the CLI reports Anthropic prices whatever
    endpoint it was pointed at. Returns (usd, source-label) and never raises:
    an unreadable figure is 0.0 with a label that says so, not a crash and not
    a silent guess at some other number.
    """
    if not output:
        return 0.0, "no-output"

    event = extract_result_event(output)
    payload = _flatten_usage(event) if event else {}

    if not payload or payload.get("total_cost_usd") is None:
        # No usable result event: fall back to scraping a cost out of any line
        # that carries one, rather than reporting zero for a run that spent.
        for line in reversed(output.splitlines()):
            if '"total_cost_usd"' not in line:
                continue
            match = _RESULT_COST_RE.search(line)
            if match:
                try:
                    return float(match.group(1)), "cli-regex"
                except ValueError:
                    pass
        return 0.0, "no-cost-line"

    try:
        reported = float(payload.get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0, "unparseable-cost"

    if _routing is None:
        return reported, "cli"

    try:
        priced = _routing.apply_backend_pricing(backend, payload)
    except Exception:
        return reported, "cli"

    try:
        computed = float(priced.get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        computed = 0.0
    source = str(priced.get("cost_source") or "cli")

    # Repricing computes from tokens. A result line that carries a cost but no
    # token counts therefore reprices to exactly $0.00 — and a room that debits
    # nothing spends without limit. This is the same failure shape as the
    # "unknown model costs $0" that T26 refused to copy, so it fails the other
    # way: keep the provider's own figure and label it, rather than book a zero
    # nobody would notice.
    if computed == 0.0 and reported > 0.0:
        return reported, f"cli-fallback-no-tokens:{backend}"

    return computed, source


def _record_cost(task_id: str, stage: str, backend: str,
                 cost_usd: float, source: str) -> None:
    """Best-effort ledger write. A ledger failure never affects the room."""
    try:
        import cost_ledger
        cost_ledger.record(task_id=task_id, stage=stage, backend=backend,
                           cost_usd=cost_usd, source=source)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Specialist prompt
# ---------------------------------------------------------------------------
def specialist_prompt(delegation: room.Delegation, request: str) -> str:
    """What the hired specialist actually reads.

    The original request goes in verbatim alongside the conductor's brief: the
    reference does the same, and for the same reason — a brief written about a
    task reads very differently from the task itself, and a specialist working
    only from the brief loses whatever the conductor did not think to restate.
    """
    profile = room.TOOL_PROFILES[delegation.tools_profile]
    return f"""\
You are a specialist hired for one job. Do that job and stop.

YOUR BRIEF
{delegation.task_instruction}

CONTEXT FROM THE CONDUCTOR
{delegation.context or "(none given)"}

THE OWNER'S ORIGINAL REQUEST, VERBATIM
{request}

YOUR TOOLS
{profile.description}

Work in the current directory. If your job is to produce a document, write it
to a file here — that file is the deliverable and it is what reaches the owner.
Finish with a short plain-text summary of what you did and what you found.
"""


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
async def run_room(
    request: str,
    *,
    room_id: str,
    workdir: str,
    spawn: Spawn,
    notify: Notify,
    budget_usd: "float | None" = None,
    max_turns: "int | None" = None,
) -> RoomOutcome:
    """Drive one room to a conclusion. Always ends with something delivered."""
    budget = room.budget_usd() if budget_usd is None else budget_usd
    turns_allowed = room.max_delegations() if max_turns is None else max_turns

    history = room.History()
    spent = 0.0
    delegations = 0
    parse_retries = 0
    turn = 1

    while True:
        remaining = budget - spent

        if remaining <= 0:
            outcome = RoomOutcome("budget", spent_usd=spent, delegations=delegations)
            await notify(
                f"Бюджет комнаты исчерпан: потрачено ${spent:.2f} из ${budget:.2f}, "
                f"специалистов отработало {delegations}. Итог не собран — "
                f"поднимите ROOM_BUDGET_USD и повторите."
            )
            return outcome

        if turn > turns_allowed:
            await notify(
                f"Лимит ходов исчерпан ({turns_allowed}). Потрачено ${spent:.2f}, "
                f"специалистов отработало {delegations}, финального ответа нет."
            )
            return RoomOutcome("turns", spent_usd=spent, delegations=delegations)

        prompt = room.build_prompt(
            request=request, history=history, attempt=turn,
            max_attempts=turns_allowed, budget_left_usd=remaining,
        )
        conductor_id = f"{room_id}-c{turn}"
        result = await spawn(task_id=conductor_id, cwd=workdir, prompt=prompt,
                             backend=CONDUCTOR_BACKEND)

        # The conductor's own turns are debited. Turn one of the live run was
        # $0.178 — 9% of the default cap before a single specialist ran.
        cost, source = parse_cost(result.output, CONDUCTOR_BACKEND)
        spent += cost
        _record_cost(conductor_id, CONDUCTOR_STAGE, CONDUCTOR_BACKEND, cost, source)

        decision = room.interpret(decision_text(result.output), history)

        if not decision.ok:
            parse_retries += 1
            if parse_retries > MAX_PARSE_RETRIES:
                await notify(
                    "Дирижёр дважды вернул ответ, который не разобрать "
                    f"({decision.error}). Останавливаюсь, чтобы не тратить "
                    f"бюджет впустую. Потрачено ${spent:.2f}."
                )
                return RoomOutcome("unparseable", summary=decision.error,
                                   spent_usd=spent, delegations=delegations)
            # The failure is already in the history with its repair hint, so the
            # next prompt shows the conductor its own mistake.
            turn += 1
            continue
        parse_retries = 0

        if decision.action == "finish":
            files = collect_deliverables(workdir)
            await notify(
                f"{decision.summary}\n\nПотрачено ${spent:.2f}, "
                f"специалистов: {delegations}.",
                files=files,
            )
            return RoomOutcome("finished", summary=decision.summary,
                               spent_usd=spent, delegations=delegations,
                               files=files)

        delegation = decision.delegation
        assert delegation is not None  # interpret() guarantees it when ok
        delegations += 1
        specialist_id = f"{room_id}-s{delegations}"
        backend = delegation.option.backend

        await notify(
            f"Специалист {delegations}: {delegation.tools_profile} "
            f"({delegation.model}). {delegation.task_instruction[:200]}"
        )

        child = await spawn(
            task_id=specialist_id, cwd=workdir,
            prompt=specialist_prompt(delegation, request), backend=backend,
        )
        child_cost, child_source = parse_cost(child.output, backend)
        spent += child_cost
        _record_cost(specialist_id, f"{SPECIALIST_STAGE}-{delegations}",
                     backend, child_cost, child_source)

        if child.rc != 0:
            # Visible, not silent: the conductor needs it for the next turn and
            # the owner needs to know a specialist fell over.
            note = f"specialist {delegations} failed (rc={child.rc})"
            history.add(room.HistoryEntry(
                "delegation", note,
                (child.output or "")[-800:] or "no output",
            ))
            await notify(f"Специалист {delegations} упал (rc={child.rc}). "
                         f"Дирижёр получит это в историю.")
        else:
            history.add(room.HistoryEntry(
                "delegation",
                f"specialist {delegations} returned (${child_cost:.3f})",
                (child.output or "")[-1500:],
            ))

        turn += 1


def collect_deliverables(workdir: str, limit: int = 5) -> "list[str]":
    """Files the specialists left behind, newest first.

    Without this the room says "the report is ready" and delivers no report:
    the documents profile writes its deliverable to disk, and disk is not the
    chat. Capped, and directories and dotfiles are skipped.
    """
    try:
        root = Path(workdir)
        found = [p for p in root.rglob("*")
                 if p.is_file() and not p.name.startswith(".")]
    except OSError:
        return []
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [str(p) for p in found[:limit]]
