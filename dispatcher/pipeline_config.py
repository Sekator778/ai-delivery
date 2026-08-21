"""A claude config directory owned by the pipeline, not by the operator.

Moving each stage's ``cwd`` into the target project (see agent_roster) fixed
*project* instructions leaking across repositories. It does nothing about the
*user* level: ``~/.claude/settings.json`` applies to every claude session on the
machine regardless of working directory, so the operator's personal interactive
setup was reaching every stage of every task. On this machine that meant:

  Stop           *            afplay ~/.claude/sounds/cat-meow.mp3
  PreToolUse     Agent|Task   bash ~/.claude/hooks/agent-limit.sh
  SubagentStart  *            bash ~/.claude/hooks/agent-limit.sh
  StopFailure    rate_limit   osascript -e 'display notification ...'
  effortLevel    xhigh

The meow is merely absurd — a batch stage playing a sound on a desktop. The
agent-limit hook is not: it caps concurrent subagents at 3 in, quoting the
script itself, "ONE GLOBAL bucket per machine (NOT per session_id)", and denies
the fourth. The Reviewer stage dispatches three lenses in parallel, tester and
security run as a pair, and the operator's own interactive session counts into
the same bucket — so a lens can be silently denied and the review comes back
clean with a hole in it. ``effortLevel: xhigh`` overrides nothing but adds cost
to every stage, on top of the pipeline's own tier routing.

So the pipeline gets its own config directory and points stages at it with
``CLAUDE_CONFIG_DIR`` (already allowlisted in child_env for exactly this).

Credentials are the one thing that must NOT be lost in the move, and the first
attempt at this module got that wrong: pointing CLAUDE_CONFIG_DIR at a fresh
directory produced "Not logged in · Please run /login" even on macOS, where the
token lives in the login Keychain. Two things have to be seeded, established by
testing rather than assumption:

  * ``.claude.json`` — account identity. Seeded from the operator's file with
    only the auth/identity keys; ``projects`` (per-repo history) and the caches
    are deliberately dropped, since carrying them would re-import some of what
    this module exists to leave behind.
  * ``.credentials.json`` — the OAuth token itself. On Linux/WSL it is already a
    file in the config dir and is copied. On macOS it lives in the login
    Keychain, and setting CLAUDE_CONFIG_DIR switches the CLI to file-backed
    storage, so it is exported from the Keychain into the new directory.

SECURITY NOTE, stated plainly because it is a real trade-off: on macOS this
writes an OAuth token to disk (mode 0600, under $HOME, never inside the repo)
that would otherwise stay in the Keychain. The isolation is worth it — an
interactive concurrency cap silently denying a review lens is a correctness
problem — but an operator who would rather not have the token on disk should set
PIPELINE_ISOLATED_CONFIG=0 and instead strip the offending hooks from their own
~/.claude/settings.json.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

# Keys copied out of the operator's ~/.claude.json. Identity and onboarding
# state only — enough for the CLI to know who it is and not prompt. Notably
# absent: "projects" (per-repo history), MCP server config, and every cache.
_ACCOUNT_KEYS = (
    "oauthAccount", "userID", "machineID", "hasCompletedOnboarding",
    "installMethod", "autoUpdates", "migrationVersion",
)

# macOS Keychain item the CLI uses when CLAUDE_CONFIG_DIR is unset.
_KEYCHAIN_SERVICE = "Claude Code-credentials"

# Re-seed the token when it expires within this window, so a long-running
# daemon does not hand a stage a credential that dies mid-stage.
_EXPIRY_MARGIN_SEC = 30 * 60

# Written into the pipeline's config dir. Deliberately almost empty: every key
# here is a decision applied to EVERY stage of EVERY task, which is what went
# wrong with inheriting the operator's file. Add one only with a reason that
# holds for unattended batch runs on any target repo.
_SETTINGS: dict = {
    # No hooks. Not "different hooks" — none. A stage runs unattended against a
    # repository the operator may never open; there is nothing for a desktop
    # notification, a sound, or an interactive concurrency guard to do here.
    "hooks": {},
}

_SETTINGS_DOC = """\
This directory is the ai-delivery pipeline's own CLAUDE_CONFIG_DIR. It exists so
stage subprocesses do not inherit the operator's personal ~/.claude (hooks,
sounds, effort level, interactive agent caps). Managed by
dispatcher/pipeline_config.py — settings.json is rewritten on every run, so
edit that module, not this file.

Auth: on macOS credentials live in the login Keychain and nothing here matters
for it. On Linux/WSL, .credentials.json is copied in from the operator's config
directory when present.
"""


def config_dir() -> Path:
    """Where the pipeline's own config lives. Override: PIPELINE_CLAUDE_CONFIG_DIR."""
    override = os.environ.get("PIPELINE_CLAUDE_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".ai-delivery" / "claude-config"


def operator_config_dir() -> Path:
    """The operator's own config dir — the one we are isolating stages FROM."""
    existing = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if existing:
        return Path(existing).expanduser()
    return Path.home() / ".claude"


def isolation_enabled() -> bool:
    """Opt out with PIPELINE_ISOLATED_CONFIG=0 (debugging a config-shaped bug)."""
    return os.environ.get("PIPELINE_ISOLATED_CONFIG", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def ensure() -> Path:
    """Create/refresh the pipeline config dir and return it.

    Idempotent and cheap enough to call per stage. Never raises: a stage that
    cannot be isolated should still run, with the operator's config, rather than
    not run at all — so every failure path falls back to the operator's dir.
    """
    target = config_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
        (target / "settings.json").write_text(
            json.dumps(_SETTINGS, indent=2) + "\n")
        (target / "README.md").write_text(_SETTINGS_DOC)
        _seed_account(operator_config_dir(), target)
        _seed_credentials(operator_config_dir(), target)
        return target
    except OSError as exc:
        print(f"warn: pipeline config dir unusable ({exc}) — stages will "
              f"inherit the operator's ~/.claude", flush=True)
        return operator_config_dir()


def _account_file(config: Path) -> Path:
    """``.claude.json`` sits beside the config dir when it is the default
    (~/.claude → ~/.claude.json) and INSIDE it when CLAUDE_CONFIG_DIR is set."""
    if config == Path.home() / ".claude":
        return Path.home() / ".claude.json"
    return config / ".claude.json"


def _seed_account(source: Path, target: Path) -> None:
    """Write a minimal ``.claude.json`` — identity without the operator's history."""
    src = _account_file(source)
    if not src.is_file():
        return
    try:
        data = json.loads(src.read_text())
    except (OSError, ValueError) as exc:
        print(f"warn: could not read {src.name} to seed pipeline identity: {exc}",
              flush=True)
        return
    minimal = {k: data[k] for k in _ACCOUNT_KEYS if k in data}
    if not minimal:
        return
    dst = target / ".claude.json"
    try:
        dst.write_text(json.dumps(minimal, indent=2) + "\n")
        os.chmod(dst, 0o600)
    except OSError as exc:
        print(f"warn: could not write the pipeline identity file: {exc}", flush=True)


def _token_is_fresh(path: Path) -> bool:
    """True when *path* holds a token good for at least the margin.

    Unreadable or unexpected shapes count as stale — re-seeding a good token is
    cheap, handing a stage a dead one costs a whole stage.
    """
    try:
        blob = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    expires = (blob.get("claudeAiOauth") or {}).get("expiresAt")
    if not isinstance(expires, (int, float)):
        return False
    # expiresAt is milliseconds since the epoch.
    return (expires / 1000.0) - time.time() > _EXPIRY_MARGIN_SEC


def _seed_credentials(source: Path, target: Path) -> None:
    """Put a usable token in the pipeline's config dir.

    Copy, not symlink: a symlink would let a refresh written by a stage
    propagate back into the operator's file, which is the coupling this module
    exists to remove.
    """
    dst = target / ".credentials.json"
    if dst.is_file() and _token_is_fresh(dst):
        return

    src = source / ".credentials.json"
    if src.is_file():  # Linux / WSL — already file-backed
        try:
            shutil.copy2(src, dst)
            os.chmod(dst, 0o600)
            return
        except OSError as exc:
            print(f"warn: could not copy credentials into the pipeline config "
                  f"dir: {exc}", flush=True)
            return

    _seed_credentials_from_keychain(dst)


def _seed_credentials_from_keychain(dst: Path) -> None:
    """macOS: export the login-Keychain item the CLI uses by default.

    Setting CLAUDE_CONFIG_DIR switches the CLI to file-backed credential
    storage, so without this the isolated directory has no token at all and
    every stage fails with "Not logged in" — which is exactly what the first
    version of this module did.
    """
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"warn: keychain lookup failed ({exc}) — stages may not be "
              f"authenticated under the isolated config", flush=True)
        return
    if proc.returncode != 0 or not proc.stdout.strip():
        print("warn: no Claude credentials in the keychain and none on disk — "
              "stages may not be authenticated under the isolated config",
              flush=True)
        return
    try:
        # Validate before writing: a malformed file is worse than none, since
        # the CLI would report a parse error instead of a clean "not logged in".
        json.loads(proc.stdout)
        dst.write_text(proc.stdout if proc.stdout.endswith("\n")
                       else proc.stdout + "\n")
        os.chmod(dst, 0o600)
    except (ValueError, OSError) as exc:
        print(f"warn: could not store the exported credential: {exc}", flush=True)


def apply_to_env(env: dict) -> dict:
    """Point *env* at the pipeline's config dir. Mutates and returns it.

    An operator who has deliberately set CLAUDE_CONFIG_DIR in their own
    environment is making a choice about where credentials live; isolation still
    applies, seeded from that directory rather than from ~/.claude.
    """
    if not isolation_enabled():
        return env
    env["CLAUDE_CONFIG_DIR"] = str(ensure())
    return env
