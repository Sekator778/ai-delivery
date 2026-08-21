"""Minimal environment for spawned `claude` children (ai-delivery-private#13).

Every pipeline stage, the triage verdict call and the bot's meta-agent run as
`claude --dangerously-skip-permissions` subprocesses. Until this module they
inherited the FULL parent environment (`os.environ.copy()`, or no `env=` at all
for the meta spawn), so any agent could dump the operator's Telegram bot token,
Windmill token, LangSmith key or owner ids with a single `env` call in its Bash
tool — a self-exfiltration surface that log redaction cannot close, because the
values are legitimately in the process environment.

`build_child_env()` inverts the default: the child receives an ALLOWLIST —

  * base POSIX vars a shell/tool needs to work at all (PATH, HOME, LANG, ...);
  * the few CLAUDE_*/tooling vars the CLI itself reads (config dir, agent
    teams flag, ssh-agent + XDG paths so `git push` / `gh` keep working);
  * the model/auth vars of the ROUTED backend only — a DeepSeek stage never
    sees GLM_API_KEY and vice versa;
  * anything the operator explicitly declares in CHILD_ENV_EXTRA (a
    comma-separated list of variable NAMES, escape hatch for host-specific
    setups such as a relocated Qdrant or a proxy).

Everything else is dropped: TELEGRAM_BOT_TOKEN, OWNER_*, TAVILY_*,
LANGSMITH_*/CC_LANGSMITH_*, MEMO_*, WINDMILL_*, LITELLM_MASTER_KEY, ...
The routed backend's own key still reaches the child — it has to, that is what
the CLI authenticates with (as ANTHROPIC_AUTH_TOKEN) — but nothing else does.

Auth that does NOT come from the environment keeps working unchanged: the
claude CLI's OAuth credentials, `gh`'s token and git identity all live in files
under HOME (or XDG_CONFIG_HOME), both of which are allowlisted.
"""
from __future__ import annotations

import os
from typing import Iterable

# ── Base POSIX / shell vars ────────────────────────────────────────────────
# Without these a spawned shell, python or git run misbehaves (no PATH, no
# HOME → no ~/.claude credentials, no TMPDIR → writes into /tmp, wrong locale
# → mojibake in artifacts). None of them is a secret.
BASE_VARS: tuple[str, ...] = (
    "PATH",
    "HOME",
    "SHELL",
    "USER",
    "LOGNAME",
    "TERM",
    "TMPDIR",
    "TZ",
    "LANG",
)

# Locale overrides come as a family (LC_ALL, LC_CTYPE, ...) — allow by prefix.
BASE_PREFIXES: tuple[str, ...] = ("LC_",)

# ── Tooling vars the CHILD (not the parent) reads ──────────────────────────
# CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS — the stage prompts spawn agent teams;
#   dropping it silently degrades every team-based stage.
# CLAUDE_CONFIG_DIR — relocates ~/.claude (credentials + settings). Unset on
#   the reference host, but dropping an operator's override would break auth.
# SSH_AUTH_SOCK — the child pushes branches; an ssh remote with a passphrase
#   key needs the agent socket (the socket path itself is not a secret).
# XDG_* — `gh` (auth token file) and other CLI tools resolve their config dir
#   through these; NPM_CONFIG_PREFIX keeps npm-global tools resolvable.
TOOL_VARS: tuple[str, ...] = (
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS",
    "CLAUDE_CONFIG_DIR",
    "SSH_AUTH_SOCK",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "NPM_CONFIG_PREFIX",
    # Build toolchain (non-secret paths). A developer/tester stage runs the
    # target repo's real build; on a multi-JDK host `mvn` picks its JDK from
    # JAVA_HOME, so dropping it would silently build against the wrong one.
    "JAVA_HOME",
    "MAVEN_HOME",
    "M2_HOME",
    "GRADLE_HOME",
    "SDKMAN_DIR",
)

# ── Per-backend model/auth vars ────────────────────────────────────────────
# Only the routed backend's family is passed through. The parent additionally
# WRITES the ANTHROPIC_* rewrite (base url / auth token / model) into the child
# env in _subagent_env — that is what the CLI actually authenticates with; the
# raw names below matter for the anthropic backend (ANTHROPIC_API_KEY when the
# operator uses a key instead of Max OAuth) and keep the model pins readable
# for a backend's own tooling.
BACKEND_VARS: dict[str, tuple[str, ...]] = {
    "anthropic": (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    ),
    "deepseek": (
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_ANTHROPIC_BASE_URL",
        "DEEPSEEK_MODEL_PRIMARY",
        "DEEPSEEK_MODEL_SONNET",
        "DEEPSEEK_MODEL_HAIKU",
        "DEEPSEEK_MODEL_SUBAGENT",
    ),
    "glm": (
        "GLM_API_KEY",
        "GLM_ANTHROPIC_BASE_URL",
        "GLM_MODEL_PRIMARY",
        "GLM_MODEL_SONNET",
        "GLM_MODEL_HAIKU",
        "GLM_MODEL_SUBAGENT",
    ),
}

# Operator escape hatch: CHILD_ENV_EXTRA=NAME1,NAME2 passes those NAMES through
# (values are read from the parent env). Use for host-specific overrides the
# allowlist cannot know about — e.g. MEMO_QDRANT_URL when Qdrant is not on the
# default port, HTTPS_PROXY, or CC_LANGSMITH_API_KEY to re-enable child-side
# tracing.
EXTRA_VAR = "CHILD_ENV_EXTRA"


def _extra_names(source: "dict[str, str]") -> list[str]:
    raw = source.get(EXTRA_VAR, "")
    return [name.strip() for name in raw.split(",") if name.strip()]


def build_child_env(
    backend: "str | None" = None,
    *,
    extra: "Iterable[str] | None" = None,
    source: "dict[str, str] | None" = None,
) -> dict[str, str]:
    """Return the minimal env for a spawned `claude`, built by allowlist.

    ``backend`` selects which model/auth family is passed through ("anthropic",
    "deepseek", "glm"); an unknown or missing backend contributes none.
    ``extra`` adds caller-declared variable names on top of CHILD_ENV_EXTRA.
    ``source`` defaults to ``os.environ`` (injectable for tests).
    """
    src = os.environ if source is None else source
    names: list[str] = [*BASE_VARS, *TOOL_VARS]
    names += [k for k in src if k.startswith(BASE_PREFIXES)]
    names += BACKEND_VARS.get((backend or "").strip().lower(), ())
    names += _extra_names(src)
    if extra:
        names += [n for n in extra if n]
    return {name: src[name] for name in names if name in src}
