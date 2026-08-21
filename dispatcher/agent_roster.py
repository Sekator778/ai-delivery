"""Persona injection + working-directory resolution for stage subprocesses.

Why this module exists (2026-08-15).

The stage subprocess used to be spawned with no ``cwd``, so it inherited the
daemon's working directory — this repo. Claude Code loads ``CLAUDE.md`` from
the working directory at startup, which meant every stage of every task booted
with *ai-delivery's* own instructions (two-remote push policy, internal language
policy) while developing a completely different project, and never saw the
target's. The framework is supposed to build applications in any language; the
language, build command and house conventions live in the target repo's own
``CLAUDE.md`` / ``AGENTS.md``, and the pipeline was the one consumer not reading
them.

The obvious fix — run the stage from the target's directory — looked blocked:
subagent personas resolve from ``.claude/agents/`` of the working directory, and
none are installed at user level, so moving ``cwd`` would break every
``subagent_type`` dispatch. It is not blocked. ``claude --agents <json>`` passes
persona definitions directly into the run, independent of the filesystem.
Verified empirically before this module was written: a directory containing only
a ``CLAUDE.md`` (and no ``.claude/`` at all) both loaded that file as project
instructions AND dispatched a persona supplied via ``--agents``.

So a stage now runs with ``cwd`` inside the target worktree, with the personas
injected and this repo added via ``--add-dir`` so the task artifacts under
``tasks/active/<id>/`` and the vendored templates under ``.claude/templates/``
stay reachable.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"

# Files in .claude/agents/ that are documentation, not personas.
_NON_PERSONA = {"README.md", "UPSTREAM"}


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a persona file into its YAML-ish frontmatter and body.

    Deliberately not a YAML parser: persona frontmatter is a flat block of
    ``key: value`` lines (name, description, tools, model), and pulling in a
    YAML dependency for that would be the only reason this pipeline needed one.
    A file without a leading ``---`` fence yields an empty mapping and its whole
    text as the body, which is the right degraded behaviour — the persona still
    works, it just carries no declared tool restriction.
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        # Frontmatter values are frequently quoted (descriptions especially).
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        meta[key.strip()] = value
    return meta, parts[2].lstrip("\n")


def _tools_list(raw: str) -> list[str]:
    """Parse the frontmatter ``tools:`` field into a list.

    The role-class tool restriction is the pipeline's main safety property for
    read-only personas — the reviewer is *physically* unable to patch code
    because its definition grants no Write/Edit/Bash. That guarantee has to
    survive the trip through --agents, so an unparseable value must not silently
    become "all tools": callers drop the key entirely instead, which leaves the
    persona at the harness default rather than at a wrongly-narrowed set.
    """
    return [t.strip() for t in raw.split(",") if t.strip()]


def build_agents_payload(agents_dir: Path | None = None) -> dict[str, dict]:
    """Build the ``--agents`` mapping from the persona files on disk.

    Returns ``{name: {description, prompt, [tools], [model]}}`` — the shape the
    claude CLI documents for that flag. The persona's frontmatter ``name``
    wins over its filename when both are present (they agree today; the rename
    of ``microservices-architect`` → ``architect`` is why the field is
    authoritative).
    """
    directory = agents_dir or AGENTS_DIR
    payload: dict[str, dict] = {}
    for path in sorted(directory.glob("*.md")):
        if path.name in _NON_PERSONA:
            continue
        meta, body = _split_frontmatter(path.read_text(errors="replace"))
        name = meta.get("name") or path.stem
        entry: dict[str, object] = {
            "description": meta.get("description", ""),
            "prompt": body.strip(),
        }
        if meta.get("tools"):
            tools = _tools_list(meta["tools"])
            if tools:
                entry["tools"] = tools
        model = meta.get("model", "").strip()
        # "inherit" is our own convention for "same capability as the calling
        # session" — it is not a model id, and passing it through would pin a
        # nonexistent model. Omitting the key is exactly what it means.
        if model and model != "inherit":
            entry["model"] = model
        payload[name] = entry
    return payload


def agents_arg(agents_dir: Path | None = None) -> list[str]:
    """``--agents <json>`` argv fragment, or empty when there are no personas."""
    payload = build_agents_payload(agents_dir)
    if not payload:
        return []
    return ["--agents", json.dumps(payload, ensure_ascii=False)]


def stage_cwd(target_repo: Path, worktree_path: "str | Path | None") -> Path:
    """Working directory for a stage subprocess.

    The task's ephemeral worktree when it exists (it is created before the
    Developer stage), otherwise the target repo itself. Both make the TARGET's
    CLAUDE.md / AGENTS.md the project instructions Claude Code loads at startup,
    which is the whole point. Falls back to this repo only when neither path is
    usable — a stage that cannot start is worse than one with the wrong project
    instructions.
    """
    for candidate in (worktree_path, target_repo):
        if not candidate:
            continue
        p = Path(candidate)
        if p.is_dir():
            return p
    return REPO_ROOT


def add_dir_args(work_dir: Path) -> list[str]:
    """``--add-dir`` fragment granting access to this repo from the target's cwd.

    The stage still reads and writes its own artifacts under
    ``tasks/active/<id>/`` and cites vendored templates under
    ``.claude/templates/`` — both live here, outside the target worktree. Skipped
    when the working directory is already this repo (the degraded fallback in
    stage_cwd), where it would be a no-op.
    """
    if work_dir == REPO_ROOT or REPO_ROOT in work_dir.parents:
        return []
    return ["--add-dir", str(REPO_ROOT)]


def describe(work_dir: Path, payload_size: int) -> str:
    """One-line operator breadcrumb for the stage log."""
    rel = os.path.basename(str(work_dir))
    return (f"cwd={work_dir} ({rel}) — target project instructions load from "
            f"here; {payload_size} personas injected via --agents")
