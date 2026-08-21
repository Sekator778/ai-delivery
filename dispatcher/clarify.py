"""C.2 — interactive clarification helpers.

Used by the BA pipeline stage (in stage_runner_agent.py) to detect remaining
`[NEEDS CLARIFICATION: ...]` markers in the BA artifact, persist them as a
pending-questions payload, and let the bot ask the operator via Telegram.

The orchestrator scans the artifact after BA completes (when
CLARIFY_INTERACTIVE_ENABLED=1); the bot reads the payload, sends an inline
prompt, collects the reply, writes the answers to `clarifications.md` in the
task dir, and bounces the task back to `tasks/inbox/` so the dispatcher
re-ingests it. On the second pass BA reads `clarifications.md` and proceeds.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

MAX_QUESTIONS = 5

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
