"""Project registry — the ONE parser for `bot/projects.json`.

Two consumers, one parser (added with #6):

  * `bot.py` — resolves `/task @alias <prompt>` to the target repo path and
    lists the registry in `/projects`.
  * `dispatcher/target_policy._base_branch` — resolves a target repo path to
    the base branch its feature branches are cut from and its PRs target.

Two entry shapes are accepted, so an existing registry keeps working untouched:

    "alias": "/abs/path"                            # legacy — path only
    "alias": {"path": "/abs/path", "base": "dev"}   # extended — per-target base

Unknown keys inside an extended entry are ignored (forward-compatible). A
malformed entry (neither a string nor a dict with a usable `path`) is dropped
rather than raising — a typo in one line must never take the whole registry,
and with it every target, offline.

Imports only stdlib; the only I/O is reading the registry file.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


# `bot/projects.json` is gitignored (paths are per-host); `projects.example.json`
# next to it is the public template. Override the location with
# PROJECTS_REGISTRY (absolute path) — used by tests and by non-standard layouts.
_DEFAULT_REGISTRY = Path(__file__).resolve().parent.parent / "bot" / "projects.json"


@dataclass(frozen=True)
class Project:
    """One registry entry. `base` is None when the target does not pin a base
    branch — the caller then falls back to its own resolution order."""
    alias: str
    path: str
    base: str | None = None


def registry_path() -> Path:
    override = os.environ.get("PROJECTS_REGISTRY", "").strip()
    return Path(override).expanduser() if override else _DEFAULT_REGISTRY


def load_registry(path: Path | None = None) -> dict:
    """Raw registry JSON. Empty dict when the file is absent, unreadable, or
    not a JSON object — callers treat that as "no registry" and fall back."""
    p = Path(path) if path is not None else registry_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def parse_projects(raw: dict | None) -> dict[str, Project]:
    """alias -> Project for both entry shapes. Malformed entries are skipped."""
    out: dict[str, Project] = {}
    if not isinstance(raw, dict):
        return out
    projects = raw.get("projects")
    if not isinstance(projects, dict):
        return out
    for alias, entry in projects.items():
        if isinstance(entry, str):
            if entry.strip():
                out[alias] = Project(alias=alias, path=entry.strip(), base=None)
            continue
        if isinstance(entry, dict):
            path = str(entry.get("path") or "").strip()
            if not path:
                continue
            base = str(entry.get("base") or "").strip() or None
            out[alias] = Project(alias=alias, path=path, base=base)
    return out


def project_paths(raw: dict | None) -> dict[str, str]:
    """alias -> path, the shape `bot.py` consumed before the extended form."""
    return {alias: p.path for alias, p in parse_projects(raw).items()}


def default_alias(raw: dict | None) -> str | None:
    """`_default` — the alias a bare `/task` falls back to."""
    if not isinstance(raw, dict):
        return None
    alias = raw.get("_default")
    return alias if isinstance(alias, str) and alias.strip() else None


def _canonical(path: str) -> str:
    """expanduser+resolve so a trailing slash / `~` / symlinked entry still
    matches an already-resolved target_repo."""
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError):
        return path


def base_for_path(target_repo: Path | str, raw: dict | None = None) -> str | None:
    """The `base` pinned for this target repo, or None when the target is not
    registered / pins no base. Path-normalized on both sides."""
    entries = parse_projects(raw if raw is not None else load_registry())
    if not entries:
        return None
    want = _canonical(str(target_repo))
    for project in entries.values():
        if project.base and _canonical(project.path) == want:
            return project.base
    return None
