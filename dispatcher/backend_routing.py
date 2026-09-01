"""Backend selection & subprocess-env construction for stage execution.

Extracted from stage_runner_agent.py (god-module split, 2026-06-04). This is the
multi-provider routing layer: which model backend runs a given stage/iteration
(_resolve_stage_backend, with the iteration-2 escalation and the tier-L
upfront-anthropic rule), the per-target Anthropic model tiering
(_resolve_anthropic_model_for_target, _apply_tier_model_routing), and the
ANTHROPIC_* env rewrite that points the claude CLI at DeepSeek / GLM
Anthropic-compatible endpoints (_subagent_env, _ensure_tool_path). The per-tier
stage wall-clock budget (_stage_timeout_for) lives here too.

Depends on target_policy for the sandbox/real-target signal; otherwise stdlib +
the triage env helper.
"""
from __future__ import annotations

import json as _json
import os as _os
import sys
from pathlib import Path

import pipeline_config as _pipeline_config
import provider_profiles as _profiles
import triage as _triage
from child_env import build_child_env
from target_policy import _is_sandbox_target


# Per-stage Agent subprocess wall-clock. A stage that exceeds this is treated as
# a slow / non-convergent stage (RC_STAGE_TIMEOUT), DISTINCT from a crash (rc=1):
# it must NOT buy a second full window via the anthropic auto-fallback, and it
# routes through the graceful handoff (PR2, committee 2026-06-02). Env-tunable.
STAGE_TIMEOUT_SEC = _triage._env_int("STAGE_TIMEOUT_SEC", 900)


# L-tier stages (full pipeline, hardest work, build/test loops) get a longer wall
# clock — the default 900s timed out a real L developer stage mid-Maven-loop
# (2026-06-03). Scales only the L tier; S/M keep the tighter default.
STAGE_TIMEOUT_SEC_L = _triage._env_int("STAGE_TIMEOUT_SEC_L", 1800)


def _stage_timeout_for(tier: "str | None") -> int:
    """Wall-clock budget for one stage, scaled by triage tier. L gets the longer
    window (heavy full-pipeline work + build/test loops); S/M keep the default."""
    return STAGE_TIMEOUT_SEC_L if (tier or "").upper() == "L" else STAGE_TIMEOUT_SEC


LITELLM_PROXY_URL = _os.environ.get("LITELLM_PROXY_URL", "").strip()


LITELLM_MASTER_KEY = _os.environ.get("LITELLM_MASTER_KEY", "").strip()


# Default BACKEND is anthropic for every stage (2026-06-07 operator decision: the
# two-model policy — Opus for ba/architect, Sonnet for the rest, all on anthropic).
# DeepSeek/GLM are no longer a default for the build/verify stages; they remain
# reachable ONLY as an explicit per-task `spec.model_routing[stage]` override or as
# the rate-limit cross-provider fallback. The Opus-vs-Sonnet split is orthogonal to
# the provider and lives in _anthropic_model_for_stage.
BACKEND: dict[str, str] = {
    "discovery": "anthropic",
    "ba": "anthropic",
    "pattern-detector": "anthropic",
    "tasks": "anthropic",
    "analyze": "anthropic",
    "edge-cases": "anthropic",
    "architect": "anthropic",
    "developer": "anthropic",
    "developer-hotfix": "anthropic",
    "tester": "anthropic",
    "security": "anthropic",
    "reviewer": "anthropic",
}


# Cheap-provider stage routing (2026-08-15). The 2026-06-07 decision moved every
# stage onto anthropic, leaving DeepSeek reachable only via an explicit per-task
# `spec.model_routing[stage]` or the rate-limit cross-provider fallback.
# DEEPSEEK_STAGES re-opens the DEFAULT door for the mechanical stages (tester
# first) while the thinking stages (ba/architect) stay on the frontier model: a
# comma list of stage names whose default backend becomes deepseek. Empty ⇒
# nothing changes, so the shipped default is still all-anthropic.
#
# Three existing safety nets still apply on top, in precedence order:
#   1. spec.model_routing[stage]                  — explicit per-task override wins;
#   2. tier L + L_TIER_ANTHROPIC_STAGES           — L build/verify starts on anthropic
#      (DeepSeek timed out a real L developer stage mid-build, 2026-06-03);
#   3. iteration >= ESCALATION_AT_ITERATION       — a stage that keeps failing
#      Reviewer escalates back to anthropic for the rest of the task.
DEEPSEEK_STAGES: frozenset[str] = frozenset(
    s.strip() for s in (_os.getenv("DEEPSEEK_STAGES") or "").split(",") if s.strip()
)


def _apply_deepseek_stage_defaults(
    backend: dict[str, str], stages: "frozenset[str]"
) -> dict[str, str]:
    """Point the default backend of *stages* at deepseek, in place.

    Skips the whole override (with a warning) when DEEPSEEK_API_KEY is unset —
    routing a stage to a keyless backend only buys a late fallback inside
    _subagent_env, and a silent one at that. Unknown stage names warn and are
    ignored rather than raising: a typo in the operator's env must not stop the
    dispatcher from booting.
    """
    if not stages:
        return backend
    if not _os.environ.get("DEEPSEEK_API_KEY", "").strip():
        print("warn: DEEPSEEK_STAGES set but DEEPSEEK_API_KEY missing — "
              "keeping the anthropic defaults", file=sys.stderr)
        return backend
    for stage in sorted(stages):
        if stage not in backend:
            print(f"warn: DEEPSEEK_STAGES lists unknown stage {stage!r} — ignored",
                  file=sys.stderr)
            continue
        backend[stage] = "deepseek"
    return backend


_apply_deepseek_stage_defaults(BACKEND, DEEPSEEK_STAGES)


# Two-model policy (2026-06-07). The expensive frontier model (Opus) runs ONLY on
# the "thinking" stages that SHAPE the task — analysis/spec (ba) and architecture
# (architect) — so the resulting tasks are maximally clear. Everything else (the
# "doing" stages: developer, tester, security, reviewer, hotfix, …) runs on the
# cheaper Sonnet. ~order-of-magnitude cheaper than the old all-Opus L pipeline that
# burned ~$18 on a one-line fix. Tunable: OPUS_STAGES (comma list),
# OPUS_ANTHROPIC_MODEL (empty ⇒ leave ANTHROPIC_MODEL unset = the CLI/account
# default Opus), LOW_TIER_ANTHROPIC_MODEL (the Sonnet id).
OPUS_STAGES: frozenset[str] = frozenset(
    s.strip() for s in (_os.getenv("OPUS_STAGES") or "ba,architect").split(",")
    if s.strip()
)


def _sonnet_model() -> str:
    return (
        _os.environ.get("LOW_TIER_ANTHROPIC_MODEL", "").strip()
        or _os.environ.get("SANDBOX_ANTHROPIC_MODEL", "").strip()
        or "claude-sonnet-4-6"
    )


def _anthropic_model_for_stage(stage: "str | None") -> str:
    """The Anthropic model id for an anthropic-backed stage under the two-model
    policy. Opus stages → OPUS_ANTHROPIC_MODEL ('' means leave ANTHROPIC_MODEL
    unset → the CLI/account default Opus). All other stages → Sonnet."""
    if stage in OPUS_STAGES:
        return _os.environ.get("OPUS_ANTHROPIC_MODEL", "").strip()  # '' ⇒ default Opus
    return _sonnet_model()


ESCALATION_AT_ITERATION = int(_os.getenv("STAGE_ESCALATION_AT_ITERATION", "2"))


ESCALATION_BACKEND = _os.getenv("STAGE_ESCALATION_BACKEND", "anthropic")


# Tier-L: run the build/verify stages on anthropic from iteration 0, not the cheap
# default. DeepSeek timed out a real L developer stage mid-build (2026-06-03); L is
# rare and cost-justified, so use the most capable backend upfront instead of
# waiting for the iteration-2 escalation. Opt out with L_TIER_FORCE_ANTHROPIC=0.
L_TIER_FORCE_ANTHROPIC = (
    _os.getenv("L_TIER_FORCE_ANTHROPIC", "1").strip().lower()
    not in ("0", "false", "no", "off", "")
)


L_TIER_ANTHROPIC_STAGES = {
    s.strip() for s in _os.getenv(
        "L_TIER_ANTHROPIC_STAGES", "developer,developer-hotfix,tester,security"
    ).split(",") if s.strip()
}


def _resolve_stage_backend(
    stage: str,
    iteration: int,
    routing: dict,
    tier: "str | None" = None,
) -> tuple[str, str | None]:
    """Pick backend for stage; escalate when a cheaper model has stalled.

    If a non-anthropic stage has failed Reviewer N times (iteration >=
    ESCALATION_AT_ITERATION), bump it to anthropic for the rest of THIS
    task. Each new task starts fresh (state.json.iteration begins at 0).
    Honors explicit per-task routing — if spec.json sets
    model_routing[stage], it's already that value and escalation only
    fires when that value is itself cheaper than anthropic.

    For tier L the build/verify stages (L_TIER_ANTHROPIC_STAGES) start on
    anthropic from iteration 0 — DeepSeek struggled and timed out on a real L
    developer stage (2026-06-03), so the most capable backend is used upfront
    rather than after the iteration-2 escalation. Explicit per-task routing and
    L_TIER_FORCE_ANTHROPIC=0 both override this.

    Returns (backend, escalated_from) — escalated_from is non-None only
    when the pick was overridden, for worklog/telegram annotation.
    """
    base = routing.get(stage, BACKEND[stage])
    if (
        L_TIER_FORCE_ANTHROPIC
        and (tier or "").upper() == "L"
        and stage in L_TIER_ANTHROPIC_STAGES
        and base != "anthropic"
        and stage not in routing          # honor an explicit per-task override
    ):
        return "anthropic", base
    if iteration >= ESCALATION_AT_ITERATION and base != ESCALATION_BACKEND:
        return ESCALATION_BACKEND, base
    return base, None


SUPPORTED_BACKENDS = ("anthropic", "deepseek", "glm")


def _ensure_tool_path(env: dict[str, str]) -> None:
    """Prepend the npm global bin to PATH so MCP servers installed via
    ``npm install -g`` (e.g. ``codegraph``) are launchable from a
    systemd-spawned claude, whose PATH otherwise omits the user prefix.
    Host-agnostic: only directories that actually exist are added, so no
    personal path is hardcoded into the repo.
    """
    extra = []
    npm_bin = _os.path.expanduser("~/.npm-global/bin")
    if _os.path.isdir(npm_bin):
        extra.append(npm_bin)
    prefix_bin = _os.environ.get("NPM_CONFIG_PREFIX", "").strip()
    if prefix_bin:
        cand = _os.path.join(prefix_bin, "bin")
        if _os.path.isdir(cand):
            extra.append(cand)
    if not extra:
        return
    current = env.get("PATH", "")
    parts = current.split(":") if current else []
    env["PATH"] = ":".join([p for p in extra if p not in parts] + parts)


def _child_env(backend: str) -> dict[str, str]:
    """Allowlisted child env for *backend* (ai-delivery-private#13) with the
    npm-global bin folded into PATH. Replaces the former ``os.environ.copy()``:
    the spawned claude no longer inherits the operator's unrelated secrets.

    Also points the stage at the pipeline's OWN claude config dir, so it stops
    inheriting the operator's personal ~/.claude — see pipeline_config for what
    that was dragging in (a desktop sound on every stage, and a machine-global
    3-subagent cap that can silently deny one of the Reviewer's three lenses).
    """
    env = build_child_env(backend)
    _ensure_tool_path(env)
    _pipeline_config.apply_to_env(env)
    return env


def _resolve_anthropic_model_for_target(target_repo: Path) -> None:
    """Per-target model tiering — set PIPELINE_ANTHROPIC_MODEL for THIS runner
    process based on the target repo, so anthropic stages (BA/architect/reviewer)
    run on the right model:
      - sandbox/validation repos  → SANDBOX_ANTHROPIC_MODEL (e.g. Sonnet: cheap,
        high session limits, plenty for test tasks)
      - real/production repos      → the operator default (Opus on Max — quality)
    An explicit global PIPELINE_ANTHROPIC_MODEL always wins (applies everywhere).
    No-op when neither env var is set (→ Opus default for all)."""
    if _os.environ.get("PIPELINE_ANTHROPIC_MODEL", "").strip():
        return  # explicit global override already pins the model for all targets
    sandbox_model = _os.environ.get("SANDBOX_ANTHROPIC_MODEL", "").strip()
    if sandbox_model and _is_sandbox_target(target_repo):
        _os.environ["PIPELINE_ANTHROPIC_MODEL"] = sandbox_model
        print(f"[agent-pipeline] sandbox target → anthropic model {sandbox_model}",
              file=sys.stderr)
    # real target: leave PIPELINE_ANTHROPIC_MODEL unset → claude default (Opus)


def _apply_tier_model_routing(target_repo: Path, tier: "str | None",
                              global_override: str) -> None:
    """Tier-based Anthropic model routing (committee decision 2026-05-31).

    Real targets run S/M tiers on the cheaper model (Sonnet) and reserve Opus
    for L (deep-reasoning) work — a measured ~3-4x saving with no quality
    regression on the validated S run. Called AFTER triage, so `tier` is known.

    Precedence (highest first):
      1. explicit global PIPELINE_ANTHROPIC_MODEL override → untouched (wins everywhere)
      2. sandbox target                                    → untouched (already its cheap model)
      3. real target, tier S/M                             → Sonnet
      4. real target, tier L / unknown                     → Opus (leave the pin unset)

    INERT while a global override is set (it returns early), so this is a no-op
    until the operator removes PIPELINE_ANTHROPIC_MODEL from the env. The Sonnet
    model is read from LOW_TIER_ANTHROPIC_MODEL, then SANDBOX_ANTHROPIC_MODEL,
    then a hard default — so existing sandbox config keeps working unchanged.
    """
    if global_override:
        return  # explicit global pin wins everywhere
    if _is_sandbox_target(target_repo):
        return  # sandbox already pinned to its cheap model by the per-target step
    low_tier_model = (
        _os.environ.get("LOW_TIER_ANTHROPIC_MODEL", "").strip()
        or _os.environ.get("SANDBOX_ANTHROPIC_MODEL", "").strip()
        or "claude-sonnet-4-6"
    )
    if tier in ("S", "M"):
        _os.environ["PIPELINE_ANTHROPIC_MODEL"] = low_tier_model
        print(f"[agent-pipeline] real target, tier {tier} → anthropic model "
              f"{low_tier_model} (Opus reserved for L)", file=sys.stderr)
    else:
        # tier L (or triage off / unknown) → Opus: leave the pin unset
        _os.environ.pop("PIPELINE_ANTHROPIC_MODEL", None)
        print(f"[agent-pipeline] real target, tier {tier or '-'} → anthropic "
              f"default (Opus)", file=sys.stderr)


def _subagent_env(backend: str, stage: "str | None" = None, *,
                  profile: "str | None" = None) -> dict[str, str]:
    """Build the subprocess env for a given backend label.

    Each non-anthropic backend rewrites ANTHROPIC_BASE_URL/AUTH_TOKEN/MODEL so
    the claude CLI proxies to that provider's Anthropic-compatible endpoint.
    Falls back to anthropic with a warning when the chosen backend's API key
    is missing — never crashes the pipeline over a missing alt-key.

    For the anthropic backend the model is chosen PER STAGE (two-model policy,
    2026-06-07): Opus for ba/architect, Sonnet for everything else — see
    _anthropic_model_for_stage. A global PIPELINE_ANTHROPIC_MODEL pin still wins
    (used to force sandbox runs entirely onto Sonnet).

    The env is built by ALLOWLIST (child_env.build_child_env, #13), not as a
    copy of os.environ: the child gets base system vars, the claude-harness
    vars, this backend's model/auth family and CHILD_ENV_EXTRA — never the
    bot token / Windmill / LangSmith / owner ids it has no use for.

    ``profile`` selects a named key profile for this provider (T15). It only
    does anything when bot/providers.json exists: the profile's key replaces
    the global one, its base_url (when set) wins over both the default endpoint
    and the LiteLLM proxy, and the backend's own ``*_API_KEY`` is dropped from
    the child env — a stage running on the alt key has no business seeing the
    main one. Without a registry the resolution returns None and every line
    below behaves exactly as it did before profiles existed.
    """
    env = _child_env(backend)
    if backend == "anthropic":
        for k in (
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "CLAUDE_CODE_SUBAGENT_MODEL",
        ):
            env.pop(k, None)
        # Precedence: an explicit global PIPELINE_ANTHROPIC_MODEL pin wins
        # everywhere (sandbox→Sonnet, or an operator override); otherwise the
        # per-stage two-model policy decides. Empty model ⇒ leave ANTHROPIC_MODEL
        # unset ⇒ the claude CLI/account default (Opus on Max).
        forced = _os.environ.get("PIPELINE_ANTHROPIC_MODEL", "").strip()
        model = forced or _anthropic_model_for_stage(stage)
        if model:
            env["ANTHROPIC_MODEL"] = model
            env["CLAUDE_CODE_SUBAGENT_MODEL"] = model
        return env

    if backend == "deepseek":
        prof = _profiles.resolve(backend, profile)
        api_key = (prof.api_key if prof
                   else _os.environ.get("DEEPSEEK_API_KEY", "").strip())
        if not api_key:
            where = f"profile {prof.name}" if prof else "DEEPSEEK_API_KEY"
            print(f"warn: no DeepSeek key from {where} — falling back to anthropic",
                  file=sys.stderr)
            return _child_env("anthropic")
        if prof:
            env.pop("DEEPSEEK_API_KEY", None)
        if prof and prof.base_url:
            env["ANTHROPIC_BASE_URL"] = prof.base_url
            env["ANTHROPIC_AUTH_TOKEN"] = api_key
            env["ANTHROPIC_MODEL"] = _os.environ.get(
                "DEEPSEEK_MODEL_PRIMARY", "deepseek-v4-pro")
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = _os.environ.get(
                "DEEPSEEK_MODEL_SONNET", "deepseek-v4-pro")
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = _os.environ.get(
                "DEEPSEEK_MODEL_HAIKU", "deepseek-v4-flash")
            env["CLAUDE_CODE_SUBAGENT_MODEL"] = _os.environ.get(
                "DEEPSEEK_MODEL_SUBAGENT", "deepseek-v4-flash")
            return env
        if LITELLM_PROXY_URL:
            if not LITELLM_MASTER_KEY:
                print("warn: LITELLM_PROXY_URL set but LITELLM_MASTER_KEY missing "
                      "— falling back to direct DeepSeek routing",
                      file=sys.stderr)
            else:
                env["ANTHROPIC_BASE_URL"] = LITELLM_PROXY_URL
                env["ANTHROPIC_AUTH_TOKEN"] = LITELLM_MASTER_KEY
                env["ANTHROPIC_MODEL"] = _os.environ.get(
                    "DEEPSEEK_MODEL_PRIMARY", "deepseek-v4-pro"
                )
                env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = _os.environ.get(
                    "DEEPSEEK_MODEL_SONNET", "deepseek-v4-pro"
                )
                env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = _os.environ.get(
                    "DEEPSEEK_MODEL_HAIKU", "deepseek-v4-flash"
                )
                env["CLAUDE_CODE_SUBAGENT_MODEL"] = _os.environ.get(
                    "DEEPSEEK_MODEL_SUBAGENT", "deepseek-v4-flash"
                )
                return env
        env["ANTHROPIC_BASE_URL"] = _os.environ.get(
            "DEEPSEEK_ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic"
        )
        env["ANTHROPIC_AUTH_TOKEN"] = api_key
        env["ANTHROPIC_MODEL"] = _os.environ.get("DEEPSEEK_MODEL_PRIMARY", "deepseek-v4-pro")
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = _os.environ.get(
            "DEEPSEEK_MODEL_SONNET", "deepseek-v4-pro"
        )
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = _os.environ.get(
            "DEEPSEEK_MODEL_HAIKU", "deepseek-v4-flash"
        )
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = _os.environ.get(
            "DEEPSEEK_MODEL_SUBAGENT", "deepseek-v4-flash"
        )
        return env

    if backend == "glm":
        prof = _profiles.resolve(backend, profile)
        api_key = (prof.api_key if prof
                   else _os.environ.get("GLM_API_KEY", "").strip())
        if not api_key:
            where = f"profile {prof.name}" if prof else "GLM_API_KEY"
            print(f"warn: no GLM key from {where} — falling back to anthropic",
                  file=sys.stderr)
            return _child_env("anthropic")
        if prof:
            env.pop("GLM_API_KEY", None)
        if prof and prof.base_url:
            env["ANTHROPIC_BASE_URL"] = prof.base_url
            env["ANTHROPIC_AUTH_TOKEN"] = api_key
            env["ANTHROPIC_MODEL"] = _os.environ.get("GLM_MODEL_PRIMARY", "glm-4.6")
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = _os.environ.get(
                "GLM_MODEL_SONNET", "glm-4.6")
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = _os.environ.get(
                "GLM_MODEL_HAIKU", "glm-4.5-flash")
            env["CLAUDE_CODE_SUBAGENT_MODEL"] = _os.environ.get(
                "GLM_MODEL_SUBAGENT", "glm-4.5-flash")
            return env
        if LITELLM_PROXY_URL:
            if not LITELLM_MASTER_KEY:
                print("warn: LITELLM_PROXY_URL set but LITELLM_MASTER_KEY missing "
                      "— falling back to direct GLM routing",
                      file=sys.stderr)
            else:
                env["ANTHROPIC_BASE_URL"] = LITELLM_PROXY_URL
                env["ANTHROPIC_AUTH_TOKEN"] = LITELLM_MASTER_KEY
                env["ANTHROPIC_MODEL"] = _os.environ.get(
                    "GLM_MODEL_PRIMARY", "glm-4.6"
                )
                env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = _os.environ.get(
                    "GLM_MODEL_SONNET", "glm-4.6"
                )
                env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = _os.environ.get(
                    "GLM_MODEL_HAIKU", "glm-4.5-flash"
                )
                env["CLAUDE_CODE_SUBAGENT_MODEL"] = _os.environ.get(
                    "GLM_MODEL_SUBAGENT", "glm-4.5-flash"
                )
                return env
        env["ANTHROPIC_BASE_URL"] = _os.environ.get(
            "GLM_ANTHROPIC_BASE_URL", "https://api.z.ai/api/anthropic"
        )
        env["ANTHROPIC_AUTH_TOKEN"] = api_key
        env["ANTHROPIC_MODEL"] = _os.environ.get("GLM_MODEL_PRIMARY", "glm-4.6")
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = _os.environ.get(
            "GLM_MODEL_SONNET", "glm-4.6"
        )
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = _os.environ.get(
            "GLM_MODEL_HAIKU", "glm-4.5-flash"
        )
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = _os.environ.get(
            "GLM_MODEL_SUBAGENT", "glm-4.5-flash"
        )
        return env

    print(f"warn: unknown backend {backend!r} — falling back to anthropic",
          file=sys.stderr)
    return _child_env("anthropic")


# ── Provider pricing — honest stage cost for non-anthropic backends ──
#
# The claude CLI prices every session at Anthropic rates no matter what
# ANTHROPIC_BASE_URL points to: a real DeepSeek tester stage cost $0.05
# (provider balance $2.44 → $2.39) while the CLI reported $1.12 — ~22×.
# That inflated figure fed cost_cap_usd, so a task could be parked having
# spent kopecks. For non-anthropic backends the honest number is computed
# from the token counts the CLI *does* report truthfully, times the
# provider's own price table.
#
# Prices are DATA, not logic: USD per 1M tokens, verified 2026-07-25
# against the provider's published rates. DeepSeek introduces peak/off-peak
# pricing on 2026-08-16, so treat these as the flat reference rates and
# override via BACKEND_PRICES_JSON (same nested shape, merged per model)
# when they move. Every recomputed figure carries its source label so
# ledger rows stay comparable after a price change.
#
# glm has no table entry yet: its stages keep the CLI figure with source
# "cli-no-price-table:glm" — the inflation stays VISIBLE in the data
# instead of silently wrong, and fixing it is adding a dict entry.

_BACKEND_PRICES: "dict[str, dict[str, dict[str, float]]]" = {
    # DeepSeek rates effective 2026-08-17, recorded at PEAK deliberately — an
    # upper bound for cost caps, not billing; peak windows 01:00-04:00 &
    # 06:00-10:00 UTC. Same wording, same numbers as the operator's
    # BACKEND_PRICES_JSON override, so code and env say one thing.
    #
    # They previously did not. atlas carried the override and priced correctly;
    # any clone without it — this container, CI, a fresh checkout — computed on
    # the pre-August table and understated DeepSeek spend roughly 3x. Found
    # while chasing a room-budget discrepancy in T29: the brief's arithmetic and
    # the code's disagreed, and the code was the one that was wrong.
    "deepseek": {
        "deepseek-v4-pro":   {"input": 1.32, "output": 3.96,
                              "cache_read": 0.044, "cache_write": 1.32},
        "deepseek-v4-flash": {"input": 0.44, "output": 1.32,
                              "cache_read": 0.014, "cache_write": 0.44},
    },
}

# Which model a backend's stage is billed as: the PRIMARY model that runs
# the orchestrator. Subagent tokens may run on the cheaper *_SUBAGENT model,
# so pricing everything at primary rates slightly OVER-counts — acceptable:
# the cost cap is a safety guard, not a billing system (control_loop), and
# the error is a few percent, not the 22× it replaces.
_BACKEND_PRIMARY_MODEL_ENV = {
    "deepseek": ("DEEPSEEK_MODEL_PRIMARY", "deepseek-v4-pro"),
    "glm": ("GLM_MODEL_PRIMARY", "glm-4.6"),
}


def _price_table() -> "dict[str, dict[str, dict[str, float]]]":
    raw = _os.environ.get("BACKEND_PRICES_JSON", "").strip()
    if not raw:
        return _BACKEND_PRICES
    try:
        override = _json.loads(raw)
    except ValueError as exc:
        print(f"warn: BACKEND_PRICES_JSON is not valid JSON ({exc}) — "
              f"using built-in prices", file=sys.stderr)
        return _BACKEND_PRICES
    merged = {b: dict(models) for b, models in _BACKEND_PRICES.items()}
    for backend, models in override.items():
        merged.setdefault(backend, {}).update(models)
    return merged


def apply_backend_pricing(backend: str, cost_info: dict) -> dict:
    """Return cost_info with an honest total_cost_usd for this backend.

    anthropic (or an empty cost_info): pass through, source "cli" — the CLI's
    figure is the provider's figure. Other backends: recompute from tokens ×
    the provider price table; the CLI's number is preserved as
    cli_reported_cost_usd. No table entry for the backend's primary model ⇒
    keep the CLI figure, labeled so the gap stays visible in ledger data.
    Never raises; never mutates its input.
    """
    if not cost_info:
        return cost_info
    out = dict(cost_info)
    if backend == "anthropic":
        out["cost_source"] = "cli"
        return out

    env_var, default = _BACKEND_PRIMARY_MODEL_ENV.get(backend, (None, None))
    model = _os.environ.get(env_var, default) if env_var else None
    prices = _price_table().get(backend, {}).get(model or "")
    if not prices:
        out["cost_source"] = f"cli-no-price-table:{backend}"
        return out

    def _tok(key: str) -> int:
        try:
            return int(cost_info.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    computed = (
        _tok("input_tokens") * prices["input"]
        + _tok("output_tokens") * prices["output"]
        + _tok("cache_read_tokens") * prices["cache_read"]
        + _tok("cache_creation_tokens") * prices["cache_write"]
    ) / 1_000_000
    out["cli_reported_cost_usd"] = cost_info.get("total_cost_usd")
    out["total_cost_usd"] = round(computed, 6)
    out["cost_source"] = f"computed:{model}"
    return out
