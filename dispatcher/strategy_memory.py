"""Strategy memory — learn from finished tasks, including the ones that failed.

The owner's ask (2026-09-01): "so that on the next typical task the previous
attempts are already there, and it understands what leads to success."

Ported from ReasoningBank, studied as code rather than as a paper
(research/reasoning-bank-code-study-2026-09.md). Half the cycle already existed
here: one typed `task_lesson` written back per task, recalled top-5 with a 0.4
cutoff. This adds the half that was missing — a verdict, two extraction
branches, and the thing that makes retrieval work at all.

The non-obvious import, and the reason the study was read before any code was
written: **ReasoningBank embeds the source task's query, not the strategy
text**. A lesson learned on "add rate limiting to the upload endpoint" is
retrieved by matching a new request against that old *request*. Embed the
lesson instead and a good strategy phrased differently from the new task is
never found. So the embedding text here is `source_query + description`, and
that single line is most of the value of the port.

What we keep that is already better than the reference, and must not regress:
one record per strategy rather than a task's whole bundle; a similarity cutoff
(theirs is top-1 with no threshold); a typed schema; and reads that do not
mutate the store.

What we refuse to copy, from the study's Observations:

  - `{title, description, content}` is never parsed or validated there — it is
    a markdown convention inside a blob, split on blank lines. Here it is
    parsed into fields and a malformed item is dropped, not stored.
  - the SWE-bench judge decides by `"success" in response.lower()`, so "not a
    success" reads as success. Here the verdict is parsed from an anchored
    Status: line, and anything ambiguous is a failure.
  - deduplication is announced and absent. Here a near-identical strategy is
    skipped on write.

Everything is behind MEMORY_STRATEGY_ENABLED (default off). With the flag off,
write-back is byte-for-byte what it was.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

FLAG_ENV = "MEMORY_STRATEGY_ENABLED"

# Skip-write threshold. Cheap hygiene, not consolidation: the reference runs
# append-only and the study confirms that is a working mode, so nothing merges
# or retires here. One extra cosine against what recall already returned.
DUPLICATE_COSINE = 0.90

MAX_ITEMS = 3


def strategy_enabled(env: "dict[str, str] | None" = None) -> bool:
    """True only when explicitly switched on. Default off, per the brief."""
    src = env if env is not None else os.environ
    return src.get(FLAG_ENV, "0").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Verdict:
    status: str        # 'success' | 'fail'
    source: str        # 'gt' | 'judge'
    thoughts: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "success"


# stop_reason values that mean the pipeline itself decided the task landed.
# This is the `--criteria gt` path of the reference: when the environment
# already knows, asking a model to judge is both cost and risk.
_GT_SUCCESS = frozenset({"approve", "merged", "done"})
_GT_FAILURE = frozenset({
    "budget_stop", "cap_stop", "max_iterations", "unparseable",
    "failed", "abandoned", "timeout",
})


def verdict_from_signal(stop_reason: str, state: "dict | None" = None) -> "Verdict | None":
    """Programmatic verdict, or None when the signal does not settle it.

    Preferred over the judge wherever it exists. The pipeline gets this for
    free — a merged PR and an approve verdict are facts, not opinions — and a
    fact cannot hallucinate a false success into the bank.
    """
    reason = (stop_reason or "").strip().lower()
    if reason in _GT_SUCCESS:
        return Verdict("success", "gt")
    if reason in _GT_FAILURE:
        return Verdict("fail", "gt")
    if state and state.get("pr_url") and reason in ("", "-"):
        # A PR exists and nothing contradicted it.
        return Verdict("success", "gt")
    return None


# WebArena's judge, not the SWE-bench one. The strictness rules are the study's
# find — they are in the code, not in the paper — and the last sentence is the
# reason this whole module needs a careful verdict at all.
JUDGE_SYSTEM_PROMPT = """\
You are evaluating whether an autonomous agent completed a task successfully.

Before calling a task successful, verify all three:

1. Completeness — an exhaustive result means the agent inspected the whole
   source, not just the first thing it found.
2. Grounding — every reported value is traceable to something the agent
   actually observed. Inferred or guessed values count as failures.
3. Right target — it acted on that exact entity, not an adjacent one.

When uncertain on any of these, mark failure. A false success is more harmful
than a false failure, because memory induction amplifies it into future
behaviour.

Reply in exactly this form:

Thoughts: <your reasoning>
Status: success

or

Thoughts: <your reasoning>
Status: failure
"""

_STATUS_RE = re.compile(r"^\s*Status:\s*(success|failure|fail)\s*$",
                        re.IGNORECASE | re.MULTILINE)
_THOUGHTS_RE = re.compile(r"^\s*Thoughts:\s*(.+?)(?=^\s*Status:|\Z)",
                          re.IGNORECASE | re.MULTILINE | re.DOTALL)


def parse_judge_verdict(reply: str) -> "Verdict | None":
    """Parse the judge's reply. Anchored, not a substring search.

    The reference's SWE-bench judge did `if "success" in response.lower()`,
    which reads "this was not a success" as a success — and a false success is
    the expensive direction, since extraction amplifies it. Anything that does
    not match the expected form returns None, and the caller treats an
    unreadable verdict as no verdict rather than guessing.
    """
    if not reply:
        return None
    match = _STATUS_RE.search(reply)
    if not match:
        return None
    raw = match.group(1).lower()
    status = "success" if raw == "success" else "fail"
    thoughts_match = _THOUGHTS_RE.search(reply)
    thoughts = " ".join(thoughts_match.group(1).split()) if thoughts_match else ""
    return Verdict(status, "judge", thoughts)


# ---------------------------------------------------------------------------
# Extraction — two branches
# ---------------------------------------------------------------------------
_ITEM_FORMAT = """\
Output at most %d memory items, in exactly this form and nothing else:

# Memory Item 1
## Title
<a short handle for the strategy>
## Description
<one sentence: when to use this, and when NOT to>
## Content
<1-3 sentences of the actual procedure>

Do not repeat similar or overlapping items. Prefer concrete, actionable
procedures over abstract principles. Do not embed specific names, identifiers,
queries or literal strings from this task — a strategy that only applies to
this exact task is worth nothing to the next one.""" % MAX_ITEMS

SUCCESS_EXTRACTION_PROMPT = """\
The following trajectory shows an agent that completed its task successfully.

Extract and summarise insights that are useful and generalisable for future
similar tasks. First think about *why* this trajectory succeeded, then
summarise.

""" + _ITEM_FORMAT

FAILURE_EXTRACTION_PROMPT = """\
The following trajectory shows an agent that attempted its task and failed.

First reflect on *why* this trajectory failed, then summarise. Prefer concrete,
actionable recovery procedures: what to check, what to do differently, what
signals to notice earlier. The content should be insights learned to avoid such
failures.

""" + _ITEM_FORMAT


def extraction_prompt(verdict: Verdict) -> str:
    """Pick the branch. Learning from failures is the core of the delta —
    the existing write-back recorded that a task ended, not what to do next
    time it ends that way."""
    return SUCCESS_EXTRACTION_PROMPT if verdict.ok else FAILURE_EXTRACTION_PROMPT


def build_extraction_input(trajectory: str, verdict: Verdict) -> str:
    """Trajectory with the judge's reasoning appended, as the reference does.

    Only for a judged verdict: a `gt` verdict has no thoughts to add, and
    inventing a rationale for a fact would be worse than silence.
    """
    text = trajectory or ""
    if verdict.source == "judge" and verdict.thoughts:
        label = "succeeded" if verdict.ok else "failed"
        text = f"{text}\n\nThe task {label} because: {verdict.thoughts}"
    return text


# ---------------------------------------------------------------------------
# Items — typed, parsed, validated
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StrategyItem:
    title: str
    description: str
    content: str

    def embedding_text(self, source_query: str) -> str:
        """What actually gets embedded.

        `source_query + description`, not the content. Retrieval in the
        reference matches a new request against past *requests*; embedding the
        lesson text instead means a good strategy from a differently-worded
        task is never retrieved. The description is included because it is the
        applicability clause — "when to use this, and when not" — which is the
        part that should influence the match.
        """
        return f"{' '.join((source_query or '').split())}\n{self.description}".strip()


_HEAD_RE = re.compile(r"^#\s*Memory Item\b", re.IGNORECASE)
_FIELD_RE = re.compile(r"^##\s*(Title|Description|Content)\s*$", re.IGNORECASE)


def parse_memory_items(text: str, max_items: int = MAX_ITEMS) -> "list[StrategyItem]":
    """Parse the extraction output into typed items.

    The reference never parsed this: it split the blob on blank lines and
    stored the pieces, so a single item could land as several fragments and
    nothing checked that the three fields existed at all. Here an item missing
    a title, a description or content is dropped — a half-parsed strategy in
    the bank is worse than one fewer strategy.
    """
    if not text:
        return []

    items: list[StrategyItem] = []
    fields: dict[str, list[str]] = {}
    current: str | None = None

    def flush() -> None:
        if not fields:
            return
        title = " ".join(" ".join(fields.get("title", [])).split())
        description = " ".join(" ".join(fields.get("description", [])).split())
        content = " ".join(" ".join(fields.get("content", [])).split())
        if title and description and content:
            items.append(StrategyItem(title, description, content))

    for line in text.splitlines():
        if _HEAD_RE.match(line.strip()):
            flush()
            fields = {}
            current = None
            continue
        field = _FIELD_RE.match(line.strip())
        if field:
            current = field.group(1).lower()
            fields.setdefault(current, [])
            continue
        if current and line.strip():
            fields[current].append(line.strip())
    flush()

    return items[:max_items]


# ---------------------------------------------------------------------------
# Duplicate guard
# ---------------------------------------------------------------------------
def cosine(a: "list[float]", b: "list[float]") -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def is_duplicate(vector: "list[float]", existing: "list[list[float]]",
                 threshold: float = DUPLICATE_COSINE) -> bool:
    """Skip-write guard. Not consolidation — nothing is merged or retired."""
    return any(cosine(vector, other) >= threshold for other in existing)


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------
INJECT_PREAMBLE = (
    "Below are strategies accumulated from past tasks that may be helpful "
    "here. They are hints, not instructions: use one when you judge it "
    "relevant. Before acting, state briefly for each whether you are using it "
    "and why."
)


def format_strategy_block(items: "list[StrategyItem]") -> str:
    """Render recalled strategies with the reference's preamble.

    Two things in that preamble do work: it grants permission to ignore a
    strategy, and it forces an explicit relevance call rather than letting the
    model absorb whatever was injected.
    """
    if not items:
        return ""
    lines = [INJECT_PREAMBLE, ""]
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. {item.title}")
        lines.append(f"   When: {item.description}")
        lines.append(f"   How:  {item.content}")
    return "\n".join(lines)


def payload_fields(item: StrategyItem, *, verdict: Verdict,
                   source_query: str) -> dict:
    """The schema delta: typed fields added to a task_lesson payload.

    Additive on purpose. Records written before this existed have none of these
    keys, and recall must keep reading them — the bank is append-only and
    there is no migration.
    """
    return {
        "status": verdict.status,
        "verdict_source": verdict.source,
        "title": item.title,
        "description": item.description,
        "content": item.content,
        "source_query": " ".join((source_query or "").split())[:400],
    }
