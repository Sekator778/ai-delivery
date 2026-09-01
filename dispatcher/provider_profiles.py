"""Named provider key profiles (backlog/T15).

Until now a provider had exactly ONE key: ``_subagent_env`` read
``DEEPSEEK_API_KEY`` / ``GLM_API_KEY`` straight from the process environment,
which systemd fills from ``bot/.env``. Two keys of the same provider — a
personal one and a work one, or someone else's key with its own quota — could
not coexist, and the cost ledger recorded *which backend* paid but never *which
key*, so spend could not be split per quota.

This module adds a registry of named profiles and resolves one to a concrete
key. It is deliberately thin:

* **The registry holds no secrets.** A profile points at a variable name
  (``api_key_env``) or a file (``api_key_file``); the value is read at
  resolution time from the environment or that file. A JSON file full of raw
  keys would just be a second ``.env`` without the protection the first one has.
* **No registry ⇒ nothing changes.** ``resolve()`` returns None, and every
  caller keeps the exact behavior it had before this module existed. That is
  the property the regression test pins.
* **The key never travels as itself.** The parent resolves it and writes only
  ``ANTHROPIC_AUTH_TOKEN`` into the child env; the profile's own variable name
  is never added to the child-env allowlist (#13), and when a profile is active
  the backend's default ``*_API_KEY`` is dropped from the child too — a stage
  running on the alt key has no business seeing the main one.

Precedence, resolved by the caller (see ``backend_routing._subagent_env``):

    1. the profile named in the task's ``model_routing`` (``"deepseek:alt"``)
    2. the operator's session default, written into the spec at ingest by the
       bot's ``/backend`` command
    3. ``defaults.<backend>`` in the registry
    4. no registry / no match → the global environment variables, as before

Registry shape (``bot/providers.json``, gitignored like ``bot/projects.json``)::

    {
      "profiles": {
        "deepseek-main": {"backend": "deepseek", "api_key_env": "DEEPSEEK_API_KEY"},
        "deepseek-alt":  {"backend": "deepseek", "api_key_env": "DEEPSEEK_API_KEY_ALT",
                          "base_url": "https://api.deepseek.com/anthropic"},
        "glm-work":      {"backend": "glm", "api_key_file": ".secrets/glm-work.key"}
      },
      "defaults": {"deepseek": "deepseek-main"}
    }

Short-lived broker-issued credentials (roadmap NEXT #7) are the next step, not
this one: profiles are static names for static keys, with attribution added.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NamedTuple

# bot/providers.json sits beside bot/projects.json, which is already gitignored.
_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "bot" / "providers.json"

PATH_ENV = "PROVIDER_PROFILES_PATH"

# A routing label may carry the profile after a colon: "deepseek:alt".
LABEL_SEP = ":"


class Profile(NamedTuple):
    """A resolved profile. ``api_key`` is empty when the registry names a
    variable or file that does not resolve — the caller then warns and falls
    back exactly as it does for a missing global key."""

    name: str
    backend: str
    api_key: str
    base_url: str = ""

    def __repr__(self) -> str:  # never let a key reach a log or a traceback
        return (f"Profile(name={self.name!r}, backend={self.backend!r}, "
                f"api_key={'<set>' if self.api_key else '<missing>'}, "
                f"base_url={self.base_url!r})")


def registry_path(path: "str | Path | None" = None) -> Path:
    if path is not None:
        return Path(path)
    override = (os.environ.get(PATH_ENV) or "").strip()
    return Path(override).expanduser() if override else _DEFAULT_PATH


def load(path: "str | Path | None" = None) -> dict:
    """The registry, or ``{}`` when it is absent or unreadable.

    Never raises: a malformed registry must degrade to "no registry" (today's
    behavior), not take the pipeline down over a stray comma."""
    p = registry_path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def split_label(value: "str | None") -> "tuple[str, str | None]":
    """``"deepseek:alt"`` → ``("deepseek", "alt")``; ``"deepseek"`` →
    ``("deepseek", None)``. Whitespace and an empty half are tolerated so a
    hand-edited spec cannot produce a backend named ``""``."""
    raw = (value or "").strip()
    if LABEL_SEP not in raw:
        return raw, None
    backend, _, profile = raw.partition(LABEL_SEP)
    return backend.strip(), (profile.strip() or None)


def for_stage(routing: "dict | None", stage: str, backend: str,
              spec_profiles: "dict | None" = None) -> "str | None":
    """The profile this stage should pay with, or None for the registry default.

    Order: the profile named on the stage's own routing label
    (``"deepseek:alt"``) wins over the task-wide ``spec.provider_profile``,
    which the bot writes from the operator's ``/backend`` selection.

    A stage escalated to anthropic (iteration ≥ 2) or forced there by the
    L-tier guard must NOT carry the deepseek profile it was routed with — the
    profile belongs to the backend actually picked, not the one requested — so
    both sources are keyed by backend and drop out when it changes."""
    routed_backend, profile = split_label((routing or {}).get(stage))
    if profile and routed_backend == backend:
        return profile
    if isinstance(spec_profiles, dict):
        chosen = (spec_profiles.get(backend) or "").strip()
        if chosen:
            return chosen
    return None


def available(path: "str | Path | None" = None) -> "list[tuple[str, str]]":
    """``[(profile_name, backend), …]`` for display. Empty without a registry."""
    profiles = load(path).get("profiles")
    if not isinstance(profiles, dict):
        return []
    return sorted(
        (name, (entry.get("backend") or "?").strip())
        for name, entry in profiles.items() if isinstance(entry, dict)
    )


def defaults(path: "str | Path | None" = None) -> dict:
    d = load(path).get("defaults")
    return d if isinstance(d, dict) else {}


def parse_selection(text: str, *, backends: "tuple[str, ...]" = (),
                    path: "str | Path | None" = None
                    ) -> "tuple[str | None, str | None]":
    """Validate an operator's ``/backend`` argument.

    Returns ``(label, None)`` for a good selection, ``(None, None)`` for a
    clear ("off"/"default"/"reset"), and ``(None, error)`` for anything else.
    Validation happens here rather than in the bot so the accepted syntax and
    the registry stay one thing (same reason project_registry is shared)."""
    raw = (text or "").strip()
    if not raw or raw.lower() in ("off", "default", "reset", "-"):
        return None, None
    backend, profile = split_label(raw)
    if backends and backend not in backends:
        return None, (f"unknown backend {backend!r} — expected one of "
                      f"{', '.join(backends)}")
    if not profile:
        return backend, None
    known = dict(available(path))
    if not known:
        return None, ("no profile registry (bot/providers.json) on this host — "
                      f"drop the ':{profile}' suffix or create one")
    if profile not in known:
        return None, (f"unknown profile {profile!r} — registry has: "
                      f"{', '.join(sorted(known))}")
    if known[profile] != backend:
        return None, (f"profile {profile!r} belongs to {known[profile]!r}, "
                      f"not {backend!r}")
    return f"{backend}{LABEL_SEP}{profile}", None


def spec_field(label: "str | None") -> dict:
    """The ``spec.provider_profile`` value for a stored selection.

    A bare backend ("deepseek") carries no profile and yields ``{}`` — the
    session default is about WHICH KEY, not which backend; stage routing is
    decided by the pipeline (and writing it into model_routing would silently
    disable the L-tier guard, which keys on a stage being absent there)."""
    backend, profile = split_label(label)
    return {backend: profile} if backend and profile else {}


def _read_key(entry: dict, source: "dict[str, str]") -> str:
    var = (entry.get("api_key_env") or "").strip()
    if var:
        return (source.get(var) or "").strip()
    key_file = (entry.get("api_key_file") or "").strip()
    if key_file:
        p = Path(key_file).expanduser()
        if not p.is_absolute():
            p = _DEFAULT_PATH.parent.parent / p
        try:
            return p.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def resolve(backend: str, name: "str | None" = None, *,
            source: "dict[str, str] | None" = None,
            path: "str | Path | None" = None) -> "Profile | None":
    """The profile to use for ``backend``, or None to keep the legacy path.

    ``name`` is the explicitly requested profile; without it the registry's
    ``defaults.<backend>`` applies. Returns None when there is no registry, no
    such profile, or the entry belongs to a different backend — every one of
    those means "behave as before" rather than "fail"."""
    registry = load(path)
    if not registry:
        return None
    profiles = registry.get("profiles")
    if not isinstance(profiles, dict):
        return None
    wanted = (name or "").strip()
    if not wanted:
        defaults = registry.get("defaults")
        if isinstance(defaults, dict):
            wanted = (defaults.get(backend) or "").strip()
    if not wanted:
        return None
    entry = profiles.get(wanted)
    if not isinstance(entry, dict):
        return None
    if (entry.get("backend") or "").strip() != backend:
        # A profile of another provider is a configuration error, not a reason
        # to hand deepseek a glm key.
        return None
    src = os.environ if source is None else source
    return Profile(
        name=wanted,
        backend=backend,
        api_key=_read_key(entry, src),
        base_url=(entry.get("base_url") or "").strip(),
    )
