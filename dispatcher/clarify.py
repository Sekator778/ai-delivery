"""C.2 — interactive clarification helpers.

Used by the BA pipeline stage (in stage_runner_agent.py) to detect remaining
`[NEEDS CLARIFICATION: ...]` markers in the BA artifact, persist them as a
pending-questions payload, and let the bot ask the operator via Telegram.

The orchestrator scans the artifact after BA completes (when
CLARIFY_INTERACTIVE_ENABLED=1); the bot reads the payload, sends an inline
prompt, collects the reply, writes the answers to `clarifications.md` in the
task dir, and bounces the task back to `tasks/inbox/` so the dispatcher
re-ingests it. On the second pass BA reads `clarifications.md` and proceeds.

Dead man (T10, 2026-08-21): nobody is obliged to answer. On 2026-08-17 both live
tasks stood in the clarify pause for ~3 hours because the questions went
unnoticed in Telegram, and the answers the operator eventually gave were the BA's
own defaults — which the spec-kit contract requires every [NEEDS CLARIFICATION]
marker to carry. The helpers below let the watcher resume such a task by ITSELF
after CLARIFY_DEADMAN_HOURS, writing "no answer, use your defaults" into the same
clarifications.md the operator would have written. Default 0 = off; the decision
logic here is pure so the watcher sweep stays a thin I/O shell.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

MAX_QUESTIONS = 5

# ── Dead-man resume (T10) ───────────────────────────────────────────────────
# state.stage while the task waits for the operator. NOT a bucket: the task dir
# lives in awaiting-input/, this label only says why it is there. The watcher
# treats it as terminal (deliberately, fix 2026-06-01) — the sweep below is the
# only thing that may move it, and only when the dead man is armed.
PAUSED_STAGE = "awaiting_clarify"
# Bumped by the watcher when it resumes a task on defaults; carried across
# re-ingest by task_dispatcher._write_state_json.
AUTO_RESUME_COUNTER = "clarify_auto_resumes"
# ONE auto-resume per task, ever. A second clarify pause on the same task means
# the BA asked again after seeing the defaults — that is a human's call, and
# looping on defaults would answer the same questions with the same answers.
AUTO_RESUME_LIMIT = 1

_MARKER_RE = re.compile(r"\[NEEDS CLARIFICATION:\s*([^\]]*)\]")
# A bare LABEL (Q1, Q12, 1, A) — not the question itself. When the bracket holds
# only a label, the BA puts the question text AFTER the marker, so we read the
# trailing sentence instead. Observed 2026-06-01: BA wrote
# "**[NEEDS CLARIFICATION: Q1]** Should --dry-run also …" and the old extractor
# surfaced the bare "Q1" to the operator, losing every actual question.
_LABEL_RE = re.compile(r"^(?:Q?\d{1,3}|[A-Za-z])$", re.IGNORECASE)


def _clean_question(raw: str) -> str:
    """Collapse newlines/whitespace and strip the leading/trailing bold markers
    so a multi-line trailing question becomes one clean line for the Telegram
    prompt. Backticks are left intact so inline code spans stay balanced."""
    t = re.sub(r"\s+", " ", raw).strip()
    return t.strip("*_ ").strip()


def extract_pending_markers(text: str) -> list[str]:
    """Return up to MAX_QUESTIONS distinct clarification questions found in
    `text`. Order is preserved; duplicates (case-insensitive) collapse to
    first occurrence.

    Handles the marker shapes the BA may emit:
      - inline:  ``[NEEDS CLARIFICATION: which retention policy?]``
        (the whole question lives in the brackets, nothing meaningful trails).
      - labeled: ``**[NEEDS CLARIFICATION: Q1]** Should --dry-run also …``
        (a bare label inside the brackets; the question text trails the marker).
      - titled:  ``**[NEEDS CLARIFICATION: FR-008 — guard shape]** The BRD permits …``
        (a descriptive HEADING inside the brackets with the real question + the
        BA's suggested default trailing it). Earlier this leaked only the heading
        to the operator; now both are surfaced as ``heading — question``."""
    seen: list[str] = []
    seen_keys: set[str] = set()
    matches = list(_MARKER_RE.finditer(text))
    for i, match in enumerate(matches):
        label = (match.group(1) or "").strip()
        # Text that trails the marker, up to the next marker or the next
        # blank-line paragraph break — where the question body + the BA's
        # suggested default live when the bracket holds only a label/heading.
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        para = text.find("\n\n", match.end(), end)
        if para != -1:
            end = para
        trailing = _clean_question(text[match.end():end])
        label_clean = _clean_question(label)

        if not label or _LABEL_RE.match(label):
            # empty or a bare label (Q1, 1, A) → the question is the trailing text
            question = trailing or label_clean
        elif len(trailing) >= 12:
            # a HEADING in the bracket with the real question (+ default) trailing
            # it → surface BOTH (was: only the heading leaked to the operator).
            question = f"{label_clean} — {trailing}"
        else:
            # substantive bracket, nothing meaningful trails → the whole question
            # lived inline in the bracket.
            question = label_clean
        if not question:
            continue
        key = question.lower()
        if key in seen_keys:
            continue
        seen.append(question)
        seen_keys.add(key)
        if len(seen) >= MAX_QUESTIONS:
            break
    return seen


def write_pending_payload(task_dir: Path, questions: list[str]) -> Path:
    """Persist the question list (with stable indices) so the bot can render
    them and the resume flow can pair answers back. Returns the path."""
    payload = {
        "task_id": task_dir.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "questions": [
            {"id": idx + 1, "question": q} for idx, q in enumerate(questions)
        ],
    }
    target = task_dir / "clarifications-pending.json"
    target.write_text(json.dumps(payload, indent=2) + "\n")
    return target


def append_answers(task_dir: Path, qa_pairs: list[dict]) -> Path:
    """Append a numbered Q→A block to clarifications.md. Each entry in
    `qa_pairs` must look like `{"question": str, "answer": str}`."""
    target = task_dir / "clarifications.md"
    lines: list[str] = []
    if not target.exists():
        lines.append("# Clarifications")
        lines.append("")
        lines.append("Operator answers to BA's remaining [NEEDS CLARIFICATION] markers.")
        lines.append("BA reads this file on re-ingest and uses answers to replace markers.")
        lines.append("")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"## {stamp}")
    lines.append("")
    for idx, entry in enumerate(qa_pairs, 1):
        lines.append(f"**Q{idx}.** {entry['question']}")
        lines.append("")
        lines.append(f"**A{idx}.** {entry['answer']}")
        lines.append("")
    with target.open("a") as fh:
        fh.write("\n".join(lines) + "\n")
    return target


def format_question_prompt(task_id: str, questions: list[str]) -> str:
    """Human-readable Telegram message body that lists the pending questions
    in a numbered form. The user replies (Telegram reply-to) with one answer
    per line, matched back by index."""
    header = (
        f"❓ Уточнение для задачи <code>{task_id}</code>\n"
        f"BA не смог разрешить эти места через defaults. Ответь "
        f"<b>reply</b>'ем на это сообщение — по одной строке на вопрос "
        f"в том же порядке.\n"
    )
    body_lines = [f"{idx}. {q}" for idx, q in enumerate(questions, 1)]
    # Honest footer (#6a, 2026-06-02): there is no inline keyboard here — the only
    # way to answer is a text reply. Each question already carries the BA's
    # suggested default in its text, so the operator can answer briefly. (The old
    # footer advertised «Использовать defaults» / «Отмена» buttons that the bot
    # never renders, which confused the operator.)
    footer = (
        "\n💬 Кнопок нет — ответь обычным <b>reply</b>'ем на это сообщение, "
        "по одной строке на каждый вопрос в том же порядке. По каждому пункту "
        "BA уже предложил обоснованный default (он указан прямо в тексте "
        "вопроса), так что можно отвечать коротко."
    )
    return header + "\n" + "\n".join(body_lines) + "\n" + footer


def parse_reply_answers(reply_text: str, expected_count: int) -> list[str]:
    """Parse the operator's reply. Strategy:
    1. If text has `1. ans`, `2: ans`, `1) ans`-style enumerations on
       separate lines, use those.
    2. Else split by non-empty lines, take first `expected_count`.
    Returns answers padded with empty strings if fewer than expected.
    """
    enum_re = re.compile(r"^\s*(\d+)\s*[\.\):]\s*(.+?)\s*$")
    enumerated: dict[int, str] = {}
    for line in reply_text.splitlines():
        m = enum_re.match(line)
        if m:
            idx = int(m.group(1))
            enumerated[idx] = m.group(2).strip()
    if enumerated:
        return [
            enumerated.get(i, "").strip() for i in range(1, expected_count + 1)
        ]

    lines = [line.strip() for line in reply_text.splitlines() if line.strip()]
    out = lines[:expected_count]
    out.extend([""] * (expected_count - len(out)))
    return out


def deadman_hours() -> float:
    """Hours to wait for the operator before resuming on BA defaults.

    ``CLARIFY_DEADMAN_HOURS``; 0 (the default) disables the dead man entirely,
    so an install that does not opt in behaves exactly as before. A malformed or
    negative value reads as 0 — the fail-safe direction is "keep waiting for the
    human", never "resume sooner than asked"."""
    raw = (os.environ.get("CLARIFY_DEADMAN_HOURS") or "").strip()
    if not raw:
        return 0.0
    try:
        hours = float(raw)
    except ValueError:
        return 0.0
    return hours if hours > 0 else 0.0


def now_iso() -> str:
    """UTC stamp in the shape the state fields use (ISO-8601, seconds)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso_to_epoch(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def paused_at_epoch(state: dict | None, task_dir: Path) -> float | None:
    """When this task entered the clarify pause, in epoch seconds.

    Two sources, both stamped AT pause time: ``state.clarify_paused_at`` (written
    by the runner's pause) and the ``created_at`` of the pending-questions
    payload (written here, and present on tasks parked before the dead man
    existed). Deliberately NOT the state.json mtime — re-ingest rewrites that
    file with carried-forward fields, which would silently reset the clock.

    None when neither stamp is readable; the caller must then leave the task
    waiting for a human rather than guess its age."""
    epoch = _iso_to_epoch((state or {}).get("clarify_paused_at"))
    if epoch is not None:
        return epoch
    try:
        payload = json.loads((task_dir / "clarifications-pending.json").read_text())
    except (OSError, ValueError):
        return None
    return _iso_to_epoch(payload.get("created_at"))


def deadman_due(state: dict | None, task_dir: Path,
                now: float | None = None) -> bool:
    """Pure decision for the watcher sweep: may this parked task be resumed on
    the BA's own defaults? True only when the dead man is armed, the task really
    is in the clarify pause, it has never been auto-resumed, and its wait has
    run past the deadline."""
    hours = deadman_hours()
    if hours <= 0:
        return False
    st = state or {}
    if str(st.get("stage") or "") != PAUSED_STAGE:
        return False
    if int(st.get(AUTO_RESUME_COUNTER) or 0) >= AUTO_RESUME_LIMIT:
        return False
    paused_at = paused_at_epoch(st, task_dir)
    if paused_at is None:
        return False
    now = time.time() if now is None else now
    return (now - paused_at) >= hours * 3600.0


def pending_questions(state: dict | None, task_dir: Path) -> list[str]:
    """The questions this task is parked on. state.clarify_pending first (the
    runner writes it with the pause), the payload file as the fallback."""
    questions = ((state or {}).get("clarify_pending") or {}).get("questions")
    if isinstance(questions, list) and questions:
        return [str(q) for q in questions]
    try:
        payload = json.loads((task_dir / "clarifications-pending.json").read_text())
    except (OSError, ValueError):
        return []
    return [str(item.get("question") or "") for item in (payload.get("questions") or [])
            if str(item.get("question") or "").strip()]


def default_answers(questions: list[str], hours: float) -> list[dict]:
    """Q→A pairs recording "nobody answered; use the default you proposed".

    The answer text is what BA reads on re-ingest, so it points BA back at its
    OWN recorded default rather than inventing one here — the spec-kit contract
    requires every [NEEDS CLARIFICATION] marker to carry a reasonable default,
    and that default is the thing the operator would have confirmed anyway."""
    waited = f"{hours:g}"
    answer = (
        f"No operator answer within {waited}h — proceed with the reasonable "
        f"default this marker already records in the BRD, and state the chosen "
        f"default explicitly in the resolved requirement. "
        f"(Automatic resume by the clarify dead man; no human confirmed this.)"
    )
    return [{"question": q, "answer": answer} for q in questions]


def _smoke() -> None:
    spec = """\
Some FRs.
- FR-001 [NEEDS CLARIFICATION: which retention?]
- FR-002 [NEEDS CLARIFICATION: which retention?]
- FR-003 [NEEDS CLARIFICATION: oauth provider]
- FR-004 [NEEDS CLARIFICATION: rate limit threshold]
- FR-005 [NEEDS CLARIFICATION: error format]
- FR-006 [NEEDS CLARIFICATION: extra one that should be capped out]
"""
    qs = extract_pending_markers(spec)
    assert qs == [
        "which retention?",
        "oauth provider",
        "rate limit threshold",
        "error format",
        "extra one that should be capped out",
    ], qs

    reply = "1. 30 days\n2. github\n3. 1000 rpm\n4. application/problem+json\n5. drop"
    answers = parse_reply_answers(reply, len(qs))
    assert answers == ["30 days", "github", "1000 rpm", "application/problem+json", "drop"], answers

    reply_freeform = "30 days\ngithub\n1000 rpm\napplication/problem+json"
    answers2 = parse_reply_answers(reply_freeform, len(qs))
    assert answers2 == ["30 days", "github", "1000 rpm", "application/problem+json", ""], answers2

    print("clarify._smoke: ok")


if __name__ == "__main__":
    _smoke()
