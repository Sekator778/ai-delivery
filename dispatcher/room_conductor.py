"""Room conductor — synthesise a specialist per task instead of staffing a room.

The owner's metaphor (STATE/DESIGN-2026-08-31-front-door.md): someone walks into
a room and states a task; a conductor works out what the question needs and puts
the right specialists on it. The follow-up edit matters more than the metaphor:
**nobody is on the payroll**. A static catalogue of fifty personas is staff — it
has to be written, maintained, and it goes stale, which is the same drift class
the functions-runtime research measured. So the room is empty by default and the
conductor writes the specialist on the spot.

Shape borrowed from AOrchestra (studied as code, not as a paper:
research/aorchestra-code-study-2026-09.md). What we take:

  - the conductor is a decision maker, not a worker: READ -> REVIEW HISTORY ->
    VERIFY -> DECIDE, one JSON action per turn;
  - a specialist is the quad <task_instruction, context, tools_profile, model>;
  - masked model names plus a price table, so the model is chosen on cost and
    stated capability rather than on brand recognition;
  - the "PREVIOUS ATTEMPT MISSED / WORKED / MUST FIX" context template, which is
    honest about what it is: retry-with-hints, not state handover.

What we deliberately do NOT take — the fragility list from that study's
Observations section, each one a real failure mode of the reference:

  1. `json.loads` with no retry: one malformed reply kills the whole task there.
     Here a parse failure is a normal turn — see parse_decision and
     ConductorLoop.
  2. An unknown action burns a turn and leaves no trace in the history, so the
     model cannot learn from it inside the task. Here every turn is recorded,
     including refusals and unparseable output.
  3. SUBTASK HISTORY grows without a cap. Here it is bounded, and the truncation
     is announced in the prompt rather than silently dropping the oldest work.
  4. A model missing from the price table costs $0. Here an unknown model is a
     validation error, not free.

Everything is behind ROOM_ENABLED (default off) and this module is pure: no
network, no bot, no subprocess. Deciding *what* to delegate is separable from
running it, and only the first half needs to be testable offline.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

FLAG_ENV = "ROOM_ENABLED"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def room_enabled(env: dict[str, str] | None = None) -> bool:
    """True only when ROOM_ENABLED is explicitly on. Default off, per the brief."""
    src = env if env is not None else os.environ
    return src.get(FLAG_ENV, "0").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Tool profiles — the static core. Profiles, not personas: a persona is written
# per task and thrown away, a profile is a capability the room actually has.
#
# Two on the MVP, and no outbound anything: no mail, no bookings, no payments.
# An outbound action needs the approve-gate that generalises the merge gate
# (the design note generalises the merge gate into an approve gate), and that
# gate does not exist yet. A room that can only read and write drafts cannot
# do damage.
# ---------------------------------------------------------------------------
TOOL_PROFILES: dict[str, str] = {
    "web-research": (
        "Search the open web and read pages. Use for facts about the world: "
        "prices, models, reviews, availability, how other people solved this. "
        "Read-only."
    ),
    "documents": (
        "Create and edit files in the task scratch directory and hand them back "
        "to the chat. Use for the deliverable itself: a report, a shortlist, a "
        "checklist, a plan. No sending anywhere."
    ),
}


# ---------------------------------------------------------------------------
# Models: masked names + a price table.
#
# AOrchestra's trick, and it survives translation because the reason holds: a
# model asked to pick "claude-opus" against "deepseek-flash" is picking a brand
# it has opinions about. Asked to pick model_1 at $0.14 against model_3 at
# $9.00, it has to reason about what the subtask is worth.
#
# Prices are per million output tokens, in USD, as an ordering signal rather
# than an invoice — the ledger does the real accounting.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ModelOption:
    backend: str          # what dispatcher/backend_routing understands
    real_name: str        # what actually gets run
    price_per_mtok: float
    strength_hint: str


# Ordered cheapest first: the mask index then carries the price ordering, which
# is the whole point of masking.
MODEL_CATALOGUE: tuple[ModelOption, ...] = (
    ModelOption("deepseek", "deepseek-v4-flash", 0.28, "fast, literal, good at extraction and summarising"),
    ModelOption("deepseek", "deepseek-v4-pro", 1.10, "solid general reasoning at low cost"),
    ModelOption("anthropic", "claude-sonnet-4-6", 6.00, "strong reasoning, careful with ambiguity and judgement"),
)


def masked_models() -> dict[str, ModelOption]:
    """{'model_1': cheapest, ...} — the names the conductor is allowed to use."""
    return {f"model_{i}": opt for i, opt in enumerate(MODEL_CATALOGUE, 1)}


def pricing_table() -> str:
    """Markdown price table for the prompt, masked names only."""
    rows = ["| name | price / Mtok | notes |", "|---|---|---|"]
    for mask, opt in masked_models().items():
        rows.append(f"| {mask} | ${opt.price_per_mtok:.2f} | {opt.strength_hint} |")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# The quad
# ---------------------------------------------------------------------------
class DelegationError(ValueError):
    """A delegation the conductor proposed that we refuse to run."""


@dataclass(frozen=True)
class Delegation:
    task_instruction: str
    context: str
    tools_profile: str
    model: str            # masked name

    @property
    def option(self) -> ModelOption:
        return masked_models()[self.model]

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "Delegation":
        """Validate the quad. Every refusal names the field and the legal values.

        Unknown model is an error rather than a $0 default — the reference
        priced anything missing from its table at zero, which is exactly the
        wrong direction for a mistake about money to fail in.
        """
        if not isinstance(params, dict):
            raise DelegationError(f"params must be an object, got {type(params).__name__}")

        instruction = str(params.get("task_instruction", "") or "").strip()
        if not instruction:
            raise DelegationError("task_instruction is required and must not be empty")

        model = str(params.get("model", "") or "").strip()
        known = masked_models()
        if model not in known:
            raise DelegationError(
                f"model {model!r} is not one of {', '.join(known)}"
            )

        # Optional in the reference; required here. A profile is what the
        # specialist is allowed to touch, and defaulting it would mean guessing
        # at capability on the model's behalf.
        profile = str(params.get("tools_profile", "") or "").strip()
        if profile not in TOOL_PROFILES:
            raise DelegationError(
                f"tools_profile {profile!r} is not one of {', '.join(TOOL_PROFILES)}"
            )

        return cls(
            task_instruction=instruction,
            context=str(params.get("context", "") or "").strip(),
            tools_profile=profile,
            model=model,
        )


# ---------------------------------------------------------------------------
# History — bounded, and failures are first-class entries
# ---------------------------------------------------------------------------
@dataclass
class HistoryEntry:
    kind: str             # 'delegation' | 'refused' | 'unparseable' | 'finish'
    summary: str
    detail: str = ""

    def render(self) -> str:
        head = f"[{self.kind}] {self.summary}"
        return f"{head}\n{self.detail}".rstrip() if self.detail else head


@dataclass
class History:
    """Delegation history, capped by characters.

    Capped because the reference's was not, and it grows linearly with attempts
    until it crowds out the task itself. Truncation is announced rather than
    silent: a conductor that cannot see its second attempt should at least know
    that a second attempt happened.

    Failures are entries. In the reference an unknown action burned a turn and
    left nothing behind, so the model had no way to notice it was doing it
    again.
    """
    entries: list[HistoryEntry] = field(default_factory=list)
    max_chars: int = 6000

    def add(self, entry: HistoryEntry) -> None:
        self.entries.append(entry)

    def render(self) -> str:
        if not self.entries:
            return "(nothing yet — this is the first attempt)"

        rendered = [e.render() for e in self.entries]
        kept: list[str] = []
        total = 0
        # Newest first while filling, so the recent turns are the ones that
        # survive; re-ordered before returning.
        for text in reversed(rendered):
            if total + len(text) > self.max_chars and kept:
                dropped = len(rendered) - len(kept)
                kept.append(
                    f"[truncated] {dropped} earlier entr"
                    f"{'y' if dropped == 1 else 'ies'} dropped to fit the history cap"
                )
                break
            kept.append(text)
            total += len(text)
        return "\n\n".join(reversed(kept))


# ---------------------------------------------------------------------------
# Parsing the conductor's reply
# ---------------------------------------------------------------------------
class DecisionParseError(ValueError):
    """The reply was not a usable decision. Carries what to say on the retry."""

    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw

    def repair_hint(self) -> str:
        return (
            "Your previous reply could not be parsed: "
            f"{self}. Reply with ONE JSON object and nothing else — no prose "
            "before or after, no markdown fence."
        )


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def parse_decision(text: str) -> dict[str, Any]:
    """Extract the single JSON decision from a model reply.

    Tolerant on the way in — a fenced block, or the first balanced object in
    loose prose — because a model wrapping JSON in an explanation is the
    ordinary case, not an error worth failing a task over. Strict about the
    result: it must be an object with a known action.
    """
    if not text or not text.strip():
        raise DecisionParseError("reply was empty", text or "")

    candidates: list[str] = []
    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break

    for blob in candidates:
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            break
    else:
        raise DecisionParseError("no valid JSON object found in the reply", text)

    action = str(parsed.get("action", "") or "").strip()
    if action not in ("delegate", "finish"):
        raise DecisionParseError(
            f"action must be 'delegate' or 'finish', got {action!r}", text
        )
    return parsed


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
CONTEXT_TEMPLATE = """\
When a previous attempt fell short, say so explicitly in `context`:

  "PREVIOUS ATTEMPT MISSED: <the gap>
   WORKED: <what to keep>
   MUST FIX: <what to do differently>"

Be honest that this is a retry with hints, not a handover: the specialist starts
fresh and sees only what you write here plus the original request."""


def build_prompt(
    request: str,
    history: History,
    attempt: int,
    max_attempts: int,
    budget_left_usd: float,
) -> str:
    """The conductor's whole prompt, rebuilt each turn (no chat history)."""
    profiles = "\n".join(f"  - {name}: {desc}" for name, desc in TOOL_PROFILES.items())

    budget_note = f"Budget left for this room: ${budget_left_usd:.2f}."
    if budget_left_usd <= 0:
        budget_note += " EXHAUSTED — you must finish now."
    elif attempt >= max_attempts:
        budget_note += " This is your LAST attempt — finish with what you have."

    return f"""\
You are the Conductor of a room of specialists. You do not do the work
yourself: you decide what needs doing, hire one specialist at a time by
describing them, and judge what comes back.

REQUEST FROM THE OWNER
{request}

DECISION PROCESS
1. READ the request and identify what would actually satisfy it.
2. REVIEW the history below: what has been tried, what came back, what failed.
3. VERIFY the work so far against the request — not against the specialist's
   own claim of success.
4. DECIDE: delegate one more specialist, or finish.

TOOL PROFILES (pick exactly one per specialist)
{profiles}

There are no outbound capabilities. Nothing can send mail, book, or spend.
If the request needs one, say so in your finish summary.

MODELS
{pricing_table()}

Price correlates with capability. Spend cheaply on extraction, summarising and
lookups; spend up for judgement, ambiguity and anything the answer hinges on.

{budget_note}
Attempt {attempt} of {max_attempts}.

HISTORY
{history.render()}

{CONTEXT_TEMPLATE}

REPLY FORMAT
Exactly one JSON object, nothing else:

{{"action": "delegate",
  "reasoning": "why this specialist, this profile, this model",
  "params": {{
    "task_instruction": "a specific, actionable brief written TO the specialist",
    "context": "what they need to know, including any previous shortfall",
    "tools_profile": "web-research | documents",
    "model": "model_1 | model_2 | model_3"}}}}

or

{{"action": "finish",
  "reasoning": "why the request is now satisfied, or why it cannot be",
  "params": {{"summary": "the answer or artifact description for the owner"}}}}
"""


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
@dataclass
class Turn:
    """One conductor turn: what it decided, or why we refused it."""
    ok: bool
    action: str = ""
    delegation: Delegation | None = None
    summary: str = ""
    error: str = ""


def interpret(reply: str, history: History) -> Turn:
    """Turn one raw model reply into a decision, recording it either way.

    The recording is the point. A refused delegation or an unparseable reply is
    appended to the history exactly like a successful one, so the next prompt
    shows the conductor its own mistake. In the reference this was the silent
    case, and a model repeating a malformed action had no way to notice.
    """
    try:
        decision = parse_decision(reply)
    except DecisionParseError as exc:
        history.add(HistoryEntry("unparseable", str(exc), exc.repair_hint()))
        return Turn(ok=False, error=str(exc))

    action = str(decision.get("action", "")).strip()
    reasoning = str(decision.get("reasoning", "") or "").strip()
    params = decision.get("params") or {}

    if action == "finish":
        summary = str(params.get("summary", "") or "").strip() if isinstance(params, dict) else ""
        history.add(HistoryEntry("finish", reasoning or "finished", summary))
        return Turn(ok=True, action="finish", summary=summary)

    try:
        delegation = Delegation.from_params(params)
    except DelegationError as exc:
        history.add(HistoryEntry(
            "refused",
            f"delegation rejected: {exc}",
            "Fix the named field and try again; the attempt was not run.",
        ))
        return Turn(ok=False, action="delegate", error=str(exc))

    history.add(HistoryEntry(
        "delegation",
        f"{delegation.tools_profile} via {delegation.model} "
        f"(${delegation.option.price_per_mtok:.2f}/Mtok)",
        delegation.task_instruction,
    ))
    return Turn(ok=True, action="delegate", delegation=delegation)


def budget_usd(env: dict[str, str] | None = None) -> float:
    """Hard cap for one room, in USD. Default $2 per the brief."""
    src = env if env is not None else os.environ
    raw = str(src.get("ROOM_BUDGET_USD", "2")).strip()
    try:
        value = float(raw)
    except ValueError:
        return 2.0
    return value if value > 0 else 2.0


def max_delegations(env: dict[str, str] | None = None) -> int:
    src = env if env is not None else os.environ
    raw = str(src.get("ROOM_MAX_DELEGATIONS", "5")).strip()
    try:
        value = int(raw)
    except ValueError:
        return 5
    return value if value > 0 else 5


def scratch_root(env: dict[str, str] | None = None) -> str:
    """Where a room's working files live.

    A room has no repository — its deliverable is a document, not a PR — so it
    gets a scratch directory instead of a target repo. Default follows the
    convention meta/CLAUDE.md already uses for research dispatches
    (~/projects/_scratch), one subdirectory per room.
    """
    src = env if env is not None else os.environ
    configured = str(src.get("ROOM_SCRATCH_ROOT", "")).strip()
    if configured:
        return configured
    return os.path.join(os.path.expanduser("~"), "projects", "_scratch", "rooms")


COST_SOURCE = "room"   # cost-ledger source tag for room spend


# ---------------------------------------------------------------------------
# Offline smoke: build the prompts for the brief's three scenarios and check
# what can be checked without spending anything.
#
# What this proves: the prompt carries the request, both profiles, the masked
# price table and no real model names, on all three shapes of task. What it
# does NOT prove is the half that needs a live model — that a cheap model gets
# picked for a lookup and a strong one for a judgement call. That is the
# conductor's judgement, not our code, and asserting it here against a canned
# reply would be testing the fixture. It belongs to the live /room acceptance.
#
#   python3 dispatcher/room_conductor.py --smoke        summary
#   python3 dispatcher/room_conductor.py --smoke -v     full prompts
# ---------------------------------------------------------------------------
# English here on purpose: this module is a public artifact and must stay
# English (CLAUDE.md §2, enforced by ops/check-cyrillic.py). The owner writes to
# the bot in Russian, and that the pipeline survives a non-ASCII request is
# worth pinning — so the Russian-input case lives in the test file, which is
# allowlisted for exactly that kind of fixture.
SMOKE_SCENARIOS = (
    ("research", "What to read about switching to an electric car in Europe"),
    ("purchase", "Shortlist a used estate car under 15000 EUR"),
    ("planning", "Draft a one-week trip to Portugal"),
)


def _smoke(verbose: bool) -> int:
    failures = 0
    for kind, request in SMOKE_SCENARIOS:
        prompt = build_prompt(request, History(), 1, max_delegations(), budget_usd())
        problems = []
        if request not in prompt:
            problems.append("request missing")
        for profile in TOOL_PROFILES:
            if profile not in prompt:
                problems.append(f"profile {profile} missing")
        for opt in MODEL_CATALOGUE:
            if opt.real_name in prompt or opt.backend in prompt:
                problems.append(f"real model name {opt.real_name} leaked")
        if "model_1" not in prompt:
            problems.append("price table missing")

        status = "ok" if not problems else "FAIL: " + "; ".join(problems)
        failures += bool(problems)
        print(f"[{kind:9s}] {len(prompt):5d} chars  {status}")
        if verbose:
            print("-" * 70)
            print(prompt)
            print("-" * 70)

    print()
    print("Prompt construction checked. Model choice — cheap for lookups, strong")
    print("for judgement — needs a live run; it is the conductor's call, not the")
    print("code's, so it is part of the /room acceptance rather than this smoke.")
    return 1 if failures else 0


if __name__ == "__main__":
    import sys as _sys
    if "--smoke" in _sys.argv:
        raise SystemExit(_smoke(verbose="-v" in _sys.argv))
    print(__doc__)
