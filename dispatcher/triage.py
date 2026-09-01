"""Adaptive complexity triage — effort-sizing / task-complexity routing.

Implements the committee RFC
(STATE/ARCH-REVIEW-2026-05-29-adaptive-triage-COMMITTEE-RFC.md): a feed-forward
layer that sizes the pipeline to the task instead of applying the maximum
pipeline to every task. It exists because a 10-line ``power()`` function cost
$16.95 / ~40 min / 3 hotfix iterations through the full 11-stage pipeline — the
machine was correct but the process bar never lowered to match the task.

Design (RFC §Q1–Q3):

* **Deterministic pre-scan (no LLM, ~free).** Extract candidate file paths from
  the task text + spec, check them against the target repo, count files, and run
  a *deterministic* path-risk check (auth / crypto / migration / CI / payment).
  Mirrors Cloudflare's ``assessRiskTier`` + ``isSecuritySensitiveFile``.
* **One small-model LLM verdict (optional, best-effort).** Feeds task text +
  pre-scan summary to a cheap model for the soft dimensions (type, clarity,
  size cross-check, confidence). Injected as a callable so this module stays
  pure and unit-testable; on any failure or when disabled, classification
  degrades gracefully to deterministic-only.
* **Policy table maps ``type × size × risk × clarity × confidence`` → tier.**
  Risk is deterministic. HARD-risk flags (auth/crypto/payment) DOMINATE — they
  force the full L pipeline regardless of size. SOFT-risk flags
  (migration/ci_cd/infra) on a small+clear change route to M (lighter stages)
  while keeping L's iteration/token budget — see ``_SOFT_RISK_FLAGS``. review /
  test / security / developer NEVER drop — only redundant upstream reasoning
  stages do.

The *policy* (thresholds, the tier→stages map, the caps, the upgrade ladder) is
a NEW operator product-decision, not a research excerpt. Defaults below are the
RFC's recommended starting points, taken from Cloudflare production telemetry
(trivial ≤10 LOC & ≤20 files; lite ≤100 LOC). Every number is overridable via
env so it can be tuned on this host's own task logs without code changes.

This module has NO claude / subprocess / network coupling — the LLM call is a
callable injected by the runner — so the entire policy is testable at $0.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# ── Tier caps (RFC §Q3 table — operator product-decision) ──
# Budget is measured in TOKENS, not dollars: this host runs through a Claude
# subscription where the per-call $ figure is notional (you pay a flat plan,
# not per request), and tokens are backend-agnostic (anthropic/deepseek/glm
# all report usage in tokens).
#
# Calibration (2026-05-30 live ACTING run tg-...-104656): the real cost is in
# the NEVER-DROP core, not the skipped upstream stages. Measured per-stage
# input+output (logged by _parse_cost): developer ~45k, tester ~44k, security
# ~40k (+ reviewer ~45k) ⇒ ONE core pass ≈ 174k tokens. The upstream stages S
# skips (discovery/ba/pattern/architect) are only ~12k each ⇒ ~48k saved (~20%
# of a pass). So the DOMINANT S-tier saving is the ITERATION cap (1 vs up to 3),
# i.e. NOT re-running the ~174k core in nitpick hotfix loops — exactly the
# $16.95 pathology, half of which was the hotfix loop.
#
# Therefore token_cap is a GENEROUS runaway ceiling (≈ iteration_cap × ~250k),
# not a tight per-tier lever — the iteration_cap + the routing are the levers.
# The metric is noisy (excludes/var. cache+subagent tokens), so the ceiling is
# set high enough not to false-abort a normal pass. All overridable via env
# (TRIAGE_{S,M,L}_TOKEN_CAP). (TODO: make a ceiling breach trigger the upgrade
# ladder rather than hard-fail — see _token_cap_exceeded.)
_TIER_TOKEN_CAP_DEFAULT = {"S": 300_000, "M": 550_000, "L": 800_000}
_TIER_ITERATION_CAP = {"S": 1, "M": 2, "L": 3}


def tier_caps(tier: str) -> dict:
    """Per-tier {iteration_cap, token_cap}. token_cap is env-overridable so it
    can be retuned on real logs without a code change."""
    tier = tier if tier in _TIER_ITERATION_CAP else "L"
    return {
        "iteration_cap": _TIER_ITERATION_CAP[tier],
        "token_cap": _env_int(f"TRIAGE_{tier}_TOKEN_CAP", _TIER_TOKEN_CAP_DEFAULT[tier]),
    }

# Stages that NEVER drop, at any tier (RFC §Key-Finding-6: savings come from
# skipping redundant UPSTREAM stages — never from skipping review/test/security).
NEVER_DROP: tuple[str, ...] = (
    "developer", "developer-hotfix", "tester", "security", "reviewer",
)

# Stage kept additionally at M (standard) — BA, so clarity failures are caught
# cheaply (RFC §Q3 note: "BA is kept in M ... only S skips BA").
M_EXTRA: tuple[str, ...] = ("ba",)

# ── Deterministic path-risk patterns (RFC §Q2 — risk is High-reliability,
#    deterministic, and dominates). Matched against candidate file paths AND the
#    raw task text, case-insensitively. Any hit ⇒ risk=high ⇒ force full. ──
RISK_PATTERNS: dict[str, str] = {
    # Bare `session`/`sessions` narrowed to auth-qualified phrases 2026-08-21,
    # same class as the `secret` removal below. This project's own vocabulary is
    # full of harmless sessions — Claude sessions, tmux sessions, the 5-hour
    # session limit, a per-stage session id — and one of them ("own session per
    # child", issue #20) was the ONLY auth hit on the reaping task. Hard risk
    # dominates, so that single word forced tier=L: every stage on Anthropic
    # instead of DeepSeek, and a $10.86 cap stop. Real auth sessions still match:
    # `session cookie` / `session fixation` / `session hijacking` / `session
    # replay` here, and `session token` / `session credential` through the
    # neighbor words they carry.
    # Bare `permission` narrowed 2026-08-28 (backlog/T19) — fifth of the same
    # class. This repo lives inside Claude Code, where permission is harness
    # vocabulary, not authentication: `--dangerously-skip-permissions`,
    # "reduce permission prompts", "the permission classifier blocked the
    # delete", "check file permissions on the worktree" — and
    # `permissions: contents: read`, a line from this project's own CI
    # workflow. Seven of eight such lines came back risk=auth, and auth is a
    # HARD flag, so each one forced tier=L on its own. It now needs an auth
    # neighbour on one side. `rbac` and `acl` still match bare — they are
    # unambiguous. `privilege escalation` is added rather than preserved: it
    # did NOT match before this change (there was no `privilege` in the
    # alternation at all), which the brief assumed otherwise.
    #
    # `check`/`checks` is deliberately NOT an accepted right-hand neighbour.
    # "permission check" is as much harness vocabulary here as auth vocabulary,
    # and there is no way to tell them apart in a regex; under-matching one
    # phrase beats keeping the false positive that motivated the whole change.
    # Bare `token` narrowed 2026-08-22 (backlog/T18) — fourth of the same class,
    # after `secret`, `session` and the payment words. In THIS project "token" is
    # the unit of the budget, not a credential: token_cap, "token budget",
    # "input/output tokens", "500k tokens", the tokenizer. T08 is an entire brief
    # about the token cap. auth is a HARD flag, so one such word forced tier=L —
    # the line "tokens, not dollars, are the budget unit on a subscription", from
    # our own test docstring, came back risk=auth. It now needs an auth-side
    # neighbour on one side or the other. The unambiguous words (oauth, jwt,
    # credential, password, sso, login) still match bare and carry most real auth
    # text anyway, including "JWT token" and "session token".
    "auth": r"\b(auth|authn|authz|login|logout|oauth|sso|"
            r"session[\s_-]?(?:cookie|fixation|hijack|replay)|"
            r"(?:access|refresh|bearer|auth|authorization|session|id|csrf|xsrf|"
            r"api|reset|verification|invite|signed|bot|oauth)[\s_-]tokens?|"
            r"personal[\s_-]access[\s_-]tokens?|"
            r"tokens?[\s_-](?:revocations?|revoke|rotations?|rotate|expiry|"
            r"expiration|refresh|validation|validate|verify|issuance|issue|"
            r"leak|theft|scopes?|secrets?|store|storage|exchange|blacklist|"
            r"introspection|endpoint|header)|"
            r"password|passwd|credential|jwt|rbac|acl|"
            r"privilege[\s_-]?escalation|"
            r"(?:user|users|access|admin|role|roles|grant|granted|api|oauth|"
            r"scope|scoped|elevated|root)[\s_-]permissions?|"
            r"permissions?[\s_-](?:escalation|elevation|grant|granted|grants|"
            r"revoke|revoked|revocation|model|boundary|boundaries|bypass)|"
            r"authoriz|authentic)\w*",
    # Bare `secret`/`secrets` removed 2026-06-07: too noisy
    # ("CI ... no repository secrets", "secret menu", "open secret" all forced
    # tier=L on tiny changes). Real crypto-secret usage almost always co-occurs
    # with stronger neighbors (`encrypt`, `keystore`, `hmac`, `jwt`/`token`/
    # `credential` from `auth`, `private_key`) — those still match here or in
    # auth and force L on actual risk.
    "crypto": r"\b(crypto|encrypt|decrypt|cipher|signing|signature|"
              r"tls|ssl|x509|keystore|hmac|hashing|bcrypt|argon2|"
              r"private[\s_-]?key)\w*",
    "migration": r"(\bmigrations?\b|\bmigrate\b|alter\s+table|drop\s+table|"
                 r"\bddl\b|flyway|liquibase|alembic|schema\s+change)",
    "ci_cd": r"(\.github/workflows|\.gitlab-ci|jenkinsfile|\.circleci|"
             r"/ci/|/cd/|\bdeploy\b|pipeline\.ya?ml|release\.ya?ml)",
    # Bare `checkout`, `subscription`, `ledger` and `charge` removed 2026-08-21
    # (backlog/T14, same class as `secret` in June and `session` in T09): all four
    # are everyday vocabulary HERE — "git checkout -b", the Anthropic
    # subscription window, the cost ledger this repo writes per stage
    # (cost_ledger.py, ops/cost-report.py), "the CLI charges every session at
    # Anthropic rates". payment is a HARD risk flag, so one such word forced
    # tier=L on any self-target task that mentioned them — the way a single
    # "session" did to the reaping task ($10.86 cap death, 2026-08-17). Each now
    # needs a payment-side qualifier; the unambiguous words (payment, billing,
    # invoice, stripe, paypal, braintree, refund, payout, chargeback) still match
    # bare, and a real phrase like "subscription billing" or "Stripe checkout"
    # keeps matching through those anyway.
    "payment": r"\b(payment|billing|invoice|stripe|paypal|braintree|refund|"
               r"payout|chargeback|"
               r"checkout[\s_-]?(?:page|flow|form|cart|funnel|session)|"
               r"subscription[\s_-]?(?:billing|fee|renewal|charge|payment|invoice)|"
               r"(?:general|payment)[\s_-]?ledger|"
               r"ledger[\s_-]?(?:entry|entries|balance|account)|"
               r"charge[\s_-]?(?:the[\s_-])?(?:card|customer|account)|"
               r"card[\s_-]?charge)\w*",
    "infra": r"\b(dockerfile|docker-compose|kubernetes|k8s|terraform|helm|"
             r"ansible|systemd|nginx|iptables|firewall)\w*",
}

# Risk-severity split (2026-06-07). Not every risk flag deserves the full L
# pipeline on a one-line change. HARD-risk flags are dangerous even when tiny —
# they DOMINATE and force L. SOFT-risk flags (migration/ci_cd/infra) elevate
# scrutiny but, on a small+clear change, route to M instead: M drops only the
# upstream architect/pattern REASONING stages (savings come from upstream — never
# review/test/security/developer, which run at EVERY tier) while we keep L's
# iteration/token budget so a risky change still gets the full convergence loop.
# Rationale: the channel-id fix (size=S, flag=migration) was force-sized to L and
# burned ~$18 on upstream reasoning + a 3-iteration loop; the real bug it would
# have shipped was caught by the REVIEWER (a core stage M keeps), not by the
# L-only architect/pattern stages. Tunable via env (comma lists) to re-balance
# on real logs without a code change.
_HARD_RISK_FLAGS: frozenset[str] = frozenset(
    (os.environ.get("TRIAGE_HARD_RISK_FLAGS") or "auth,crypto,payment").split(",")
)
_SOFT_RISK_FLAGS: frozenset[str] = frozenset(
    (os.environ.get("TRIAGE_SOFT_RISK_FLAGS") or "migration,ci_cd,infra").split(",")
)

# Verbs that imply a genuinely tiny change (deterministic S signal).
_SMALL_SCOPE_RE = re.compile(
    r"\b(add|adds|adding|create|fix|fixes|fixing|rename|renames|"
    r"update|updates|tweak|adjust|bump|remove|removes|delete|deletes|"
    r"format|typo|comment|docstring|append|wire|expose|return)\b",
    re.IGNORECASE,
)
# Verbs that imply a large change — never trivial, lean L.
_LARGE_SCOPE_RE = re.compile(
    r"\b(refactor|rewrite|redesign|re-?architect|overhaul|migrate|port|"
    r"integrat\w+|restructure|decompose|introduce|implement\s+a\s+new|"
    r"build\s+a\s+new|end-to-end|across\s+the)\b",
    re.IGNORECASE,
)

# Explicit small-bounded-scope statements (GH issue #5, item 2): the prompt
# ITSELF asserts a tiny, deterministically-bounded change ("one file", "single
# line", "3 lines", "only .gitignore", "exactly these files") — a stronger,
# author-asserted signal than the general verb match above (_SMALL_SCOPE_RE).
# Used in classify() as a fast-path override so a soft-risk keyword hit alone
# can't keep a one-line chore at tier M (see _mask_identifiers() below for the
# companion fix to the risk side of the same incident).
_EXPLICIT_SMALL_SCOPE_RE = re.compile(
    r"\b(?:one|1|a\s+single|single)\s+(?:file|line)\b"
    r"|\b\d+[\s-]*lines?\b"
    r"|\bonly\s+(?:the\s+)?[\w./\\-]*\.\w+\b"
    r"|\b(?:exactly|only)\s+(?:these|those|\d+)\s+files?\b",
    re.IGNORECASE,
)

# Candidate source-file path extractor (extensions kept conservative so prose
# words with dots don't match). Used by the deterministic pre-scan.
_PATH_RE = re.compile(
    r"`?([\w./\-]+\.(?:py|js|ts|tsx|jsx|mjs|cjs|java|kt|go|rs|rb|c|cc|cpp|"
    r"h|hpp|cs|swift|php|scala|sql|ya?ml|toml|json|md|sh|tf|gradle|xml))`?",
    re.IGNORECASE,
)

# ── Identifier / code-span masking (GH issue #5) ───────────────────────────
# Risk keywords must not fire on a risk word that is only a FRAGMENT of an
# identifier — a backtick-quoted code span, a file/URL path, or a kebab/snake
# token like a branch name — rather than actual prose risk. The concrete false
# positive: a prompt quoting the branch name 'mac-migration' matched the
# soft-risk 'migration' keyword (\bmigrations?\b fires on the word boundary at
# the hyphen) and kept a one-line .gitignore chore at tier M. Each pattern
# below is masked out (replaced with a space — never just deleted, so words on
# either side don't fuse into a new accidental token) BEFORE the risk regexes
# run — see _mask_identifiers() / _detect_risk(). The real target files
# extracted by _candidate_paths() above are unaffected: they are appended to
# the risk-scan haystack unmasked, so a genuinely risky target path (e.g.
# auth/session.py) still forces risk=high.
_BACKTICK_SPAN_RE = re.compile(r"`[^`\n]*`")
_URL_RE = re.compile(r"\bhttps?://\S+", re.IGNORECASE)
_PATH_TOKEN_RE = re.compile(r"\S*[/\\]\S*")
# Hyphen/underscore-joined identifier (branch name, env var, slug) — real
# prose risk phrases are space-separated ("DB migration"), never hyphenated.
_BRANCH_TOKEN_RE = re.compile(r"\b\w+(?:[-_]\w+)+\b")


def _mask_identifiers(text: str) -> str:
    """Strip code-identifier spans from ``text`` before a risk-keyword scan.

    Pure and independently unit-testable (each pattern masks in isolation, so
    tests can target backticks / paths / branch tokens / URLs one at a time)."""
    text = _BACKTICK_SPAN_RE.sub(" ", text or "")
    text = _URL_RE.sub(" ", text)
    text = _PATH_TOKEN_RE.sub(" ", text)
    text = _BRANCH_TOKEN_RE.sub(" ", text)
    return text


_SIZE_ORDER = {"S": 0, "M": 1, "L": 2}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


@dataclass
class Thresholds:
    """Tunable cutoffs (RFC §Q3 [Operator product-decision]). Defaults are the
    Cloudflare-derived starting points; override via env to retune on real logs."""
    s_max_files: int = 20        # Cloudflare trivial: ≤20 files
    s_max_loc: int = 10          # Cloudflare trivial: ≤10 changed lines
    m_max_loc: int = 100         # Cloudflare lite: ≤100 changed lines
    conf_threshold: float = 0.7  # S requires confidence ≥ this (else fail to L)
    # On a scan↔LLM SIZE disagreement, confidence is capped at THIS floor — not
    # below the fail-safe threshold. Defaulting it to conf_threshold means a
    # disagreement is routed by the (already overestimate-biased, see _max_size)
    # size, instead of being silently force-failed to L. With the old inline 0.6
    # cap sitting under the 0.7 floor, ANY disagreement → L, which made the M
    # tier structurally near-unreachable (0/16 runs ever hit M up to 2026-05-31:
    # a genuine size=M/risk=low/clarity=clear task was force-routed to L on conf
    # 0.60 < 0.70). Set BELOW conf_threshold to restore "disagreement always → L".
    disagree_conf_cap: float = 0.7

    @classmethod
    def from_env(cls) -> "Thresholds":
        conf_thr = _env_float("TRIAGE_CONF_THRESHOLD", 0.7)
        return cls(
            s_max_files=_env_int("TRIAGE_S_MAX_FILES", 20),
            s_max_loc=_env_int("TRIAGE_S_MAX_LOC", 10),
            m_max_loc=_env_int("TRIAGE_M_MAX_LOC", 100),
            conf_threshold=conf_thr,
            # Tracks conf_threshold by default so raising the threshold does not
            # silently re-arm the force-L-on-disagreement squeeze.
            disagree_conf_cap=_env_float("TRIAGE_DISAGREE_CONF_CAP", conf_thr),
        )


@dataclass
class PreScan:
    """Deterministic, no-LLM scan output (RFC §Q1 input 1)."""
    files: list[str] = field(default_factory=list)      # candidate paths in text/spec
    existing: list[str] = field(default_factory=list)   # of those, present in repo
    missing: list[str] = field(default_factory=list)
    file_count: int = 0                                 # distinct files implied to touch
    loc_existing: int = 0                               # total LOC of named existing files (blast-radius proxy only)
    risk_flags: list[str] = field(default_factory=list)
    risk: str = "low"                                   # "high" if any flag else "low"
    small_scope: bool = False
    large_scope: bool = False
    explicit_small_scope: bool = False                  # "one file" / "3 lines" / "only x.ext"


@dataclass
class Verdict:
    """Soft LLM dimensions (RFC §Q1 input 2). ``ok`` is True only when a verdict
    was actually parsed — callers must treat an un-ok verdict as 'no LLM signal'."""
    is_dev_task: bool = True
    type: str = "feature"        # bugfix | feature | refactor | chore
    size: str = "M"              # S | M | L
    clarity: str = "clear"       # clear | underspecified
    confidence: float = 0.5      # 0–1, self-reported (treated as a routing knob, not truth)
    raw: str = ""
    ok: bool = False


@dataclass
class Triage:
    """Final classification written to ``state.triage``."""
    tier: str                            # S | M | L
    verdict: str                         # "dev" | "non-dev" (state-stub compat)
    estimate: str                        # S | M | L         (state-stub compat)
    dimensions: dict                     # {type, size, risk, clarity}
    confidence: float
    reasons: list[str]
    caps: dict                           # {iteration_cap, token_cap}
    source: str                          # "deterministic" | "llm+deterministic"

    def to_state(self, mode: str) -> dict:
        """Shape persisted under ``state.triage`` — a superset of the existing
        ``{verdict, estimate}`` stub so nothing that already reads those breaks."""
        return {
            "verdict": self.verdict,
            "estimate": self.estimate,
            "tier": self.tier,
            "dimensions": self.dimensions,
            "confidence": round(self.confidence, 3),
            "reasons": self.reasons,
            "caps": self.caps,
            "source": self.source,
            "mode": mode,
        }


# ── Deterministic pre-scan ────────────────────────────────────────────────

def _candidate_paths(text: str, spec: dict | None) -> list[str]:
    paths: list[str] = []
    for m in _PATH_RE.finditer(text or ""):
        p = m.group(1).strip("`")
        if p and p not in paths:
            paths.append(p)
    # spec may carry explicit hints (best-effort, never required)
    if spec:
        for key in ("files", "paths", "touch"):
            val = spec.get(key)
            if isinstance(val, list):
                for p in val:
                    if isinstance(p, str) and p not in paths:
                        paths.append(p)
    return paths


def _detect_risk(text: str, paths: list[str]) -> list[str]:
    # Mask identifier/code spans out of the free-form prose BEFORE scanning so
    # a risk word embedded in a branch name / backtick span / path / URL can't
    # fire (GH issue #5). ``paths`` — the already-extracted, real candidate
    # target files — are appended unmasked: those are legitimate risk signal.
    masked = _mask_identifiers(text or "")
    haystack = "\n".join([masked] + paths).lower()
    flags: list[str] = []
    for name, pat in RISK_PATTERNS.items():
        if re.search(pat, haystack, re.IGNORECASE):
            flags.append(name)
    return flags


def prescan(prompt: str, target_repo: Path | None, spec: dict | None = None) -> PreScan:
    """Deterministic scan over the task text + (optionally) the target repo.

    Pure except for read-only ``stat``/line-count on named existing files. Never
    raises — a missing/unreadable repo just yields an empty existing-files set."""
    text = prompt or ""
    paths = _candidate_paths(text, spec)
    existing: list[str] = []
    missing: list[str] = []
    loc = 0
    if target_repo is not None:
        for rel in paths:
            try:
                fp = (target_repo / rel)
                if fp.is_file():
                    existing.append(rel)
                    try:
                        loc += sum(1 for _ in fp.open("r", errors="replace"))
                    except OSError:
                        pass
                else:
                    missing.append(rel)
            except (OSError, ValueError):
                missing.append(rel)
    else:
        missing = list(paths)

    flags = _detect_risk(text, paths)
    small = bool(_SMALL_SCOPE_RE.search(text))
    large = bool(_LARGE_SCOPE_RE.search(text))
    explicit_small = bool(_EXPLICIT_SMALL_SCOPE_RE.search(text))
    return PreScan(
        files=paths,
        existing=existing,
        missing=missing,
        file_count=len(paths),
        loc_existing=loc,
        risk_flags=flags,
        risk="high" if flags else "low",
        small_scope=small,
        large_scope=large,
        explicit_small_scope=explicit_small,
    )


# ── LLM verdict (best-effort, injected runner) ────────────────────────────

_VERDICT_PROMPT = """\
You are the TRIAGE stage of an automated software-delivery pipeline. Classify the
task below so the pipeline can size its effort to it. Do NOT solve the task.

TASK (verbatim):
{prompt}

DETERMINISTIC PRE-SCAN (already computed, trust it):
- candidate files mentioned: {files}
- of those, exist in repo: {existing}
- deterministic risk flags (auth/crypto/migration/ci/payment/infra): {risk_flags}

Reply with ONE fenced ```json block and nothing else:
```json
{{"is_dev_task": true, "type": "bugfix|feature|refactor|chore",
  "size": "S|M|L", "clarity": "clear|underspecified", "confidence": 0.0}}
```
Sizing rubric (coarse, directional only): S = a few lines in ≤1 file; M = a
contained change in a handful of files; L = cross-module / new subsystem /
ambiguous. When unsure, size UP and lower confidence — underestimating a hard
task is far more expensive than overestimating an easy one."""


def build_verdict_prompt(prompt: str, pre: PreScan) -> str:
    return _VERDICT_PROMPT.format(
        prompt=(prompt or "").strip()[:4000],
        files=", ".join(pre.files) or "(none named)",
        existing=", ".join(pre.existing) or "(none)",
        risk_flags=", ".join(pre.risk_flags) or "(none)",
    )


def parse_verdict(raw: str) -> Verdict:
    """Parse the model's JSON reply. Tolerant: finds the first JSON object, fills
    defaults for missing keys. Returns an un-ok Verdict on any failure."""
    if not raw:
        return Verdict(ok=False, raw=raw or "")
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return Verdict(ok=False, raw=raw)
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return Verdict(ok=False, raw=raw)
    size = str(data.get("size", "M")).strip().upper()
    if size not in _SIZE_ORDER:
        size = "M"
    clarity = str(data.get("clarity", "clear")).strip().lower()
    if clarity not in ("clear", "underspecified"):
        clarity = "clear"
    typ = str(data.get("type", "feature")).strip().lower()
    if typ not in ("bugfix", "feature", "refactor", "chore"):
        typ = "feature"
    try:
        conf = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, conf))
    return Verdict(
        is_dev_task=bool(data.get("is_dev_task", True)),
        type=typ, size=size, clarity=clarity, confidence=conf,
        raw=raw, ok=True,
    )


def llm_verdict(prompt: str, pre: PreScan, run_claude) -> Verdict:
    """Run the cheap LLM verdict via the injected ``run_claude(prompt) -> str``.

    Best-effort: any exception or empty output yields an un-ok Verdict so the
    caller falls back to deterministic-only classification."""
    try:
        raw = run_claude(build_verdict_prompt(prompt, pre))
    except Exception:  # noqa: BLE001 — triage must never crash the pipeline
        return Verdict(ok=False)
    return parse_verdict(raw or "")


# ── Classification (the Q3 policy table — pure) ───────────────────────────

def _det_size(pre: PreScan, thr: Thresholds) -> str:
    """Coarse deterministic size estimate from the pre-scan. Pre-implementation
    we have no diff, so this is directional only (RFC: LLM/heuristic sizing is
    ~0.34 Spearman — good for coarse routing, never precise)."""
    if pre.large_scope:
        return "L"
    if pre.file_count > thr.s_max_files:
        return "L"
    # A trivial change must name at least ONE concrete file. With zero named
    # files the task is too vague to size deterministically ("fix something") —
    # never call it S; let it fall to M and the clarity/confidence gate route it
    # up (it lands at L as underspecified). Deterministic S needs a file anchor.
    if pre.file_count == 0:
        return "M"
    if pre.file_count <= 3 and pre.small_scope:
        return "S"
    return "M"


def _max_size(a: str, b: str) -> str:
    """Return the larger of two sizes (bias-to-overestimate, RFC §Q5)."""
    return a if _SIZE_ORDER.get(a, 1) >= _SIZE_ORDER.get(b, 1) else b


def _derive_dimensions(
    pre: PreScan, verdict: Verdict | None, thr: Thresholds,
) -> tuple[dict, float, str]:
    risk = pre.risk  # always deterministic
    det_size = _det_size(pre, thr)
    if verdict is not None and verdict.ok:
        source = "llm+deterministic"
        typ = verdict.type
        clarity = verdict.clarity
        size = _max_size(verdict.size, det_size)  # overestimate bias
        conf = verdict.confidence
        if verdict.size != det_size:
            # Disagreement lowers confidence to the fail-safe FLOOR, not below it
            # (thr.disagree_conf_cap, default = conf_threshold). The size is
            # already pushed UP by _max_size above, so an S↔M disagreement should
            # land at M — routed by that over-estimated size — not be force-failed
            # to L on a redundant confidence penalty. Risk-high dominance and
            # size=='L' both fire BEFORE the confidence gate in classify(), so an
            # over-large or risky disagreement still goes to L. See
            # Thresholds.disagree_conf_cap.
            conf = min(conf, thr.disagree_conf_cap)
    else:
        source = "deterministic"
        typ = "feature"
        size = det_size
        # Deterministic confidence comes from pre-scan unambiguity (RFC §Q2:
        # "pre-scan agreement" is a confidence source). A clearly-tiny task gets
        # enough confidence to qualify for S with no LLM call — the judge-cost
        # mitigation. Anything ambiguous stays below threshold ⇒ M/L.
        if (det_size == "S" and risk == "low"
                and pre.small_scope and not pre.large_scope
                and pre.file_count <= thr.s_max_files):
            clarity = "clear"
            conf = max(thr.conf_threshold, 0.8)
        else:
            clarity = "clear" if pre.file_count > 0 and not pre.large_scope else "underspecified"
            conf = 0.5
    dims = {"type": typ, "size": size, "risk": risk, "clarity": clarity}
    return dims, conf, source


def classify(
    pre: PreScan, verdict: Verdict | None = None, thr: Thresholds | None = None,
) -> Triage:
    """Apply the RFC §Q3 policy table. Deterministic, pure, fully unit-testable.

    Tier rules (verbatim from the RFC):
      * risk=high (deterministic) → L  (dominates everything)
      * size=L OR clarity=underspecified OR confidence < threshold → L
      * size=S AND risk=low AND clarity=clear AND confidence ≥ threshold → S
      * otherwise → M (the standard default)
    """
    thr = thr or Thresholds.from_env()
    dims, conf, source = _derive_dimensions(pre, verdict, thr)
    risk, size, clarity, typ = dims["risk"], dims["size"], dims["clarity"], dims["type"]
    reasons: list[str] = []

    non_dev = bool(verdict is not None and verdict.ok and not verdict.is_dev_task)
    hard_flags = [f for f in pre.risk_flags if f in _HARD_RISK_FLAGS]
    soft_flags = [f for f in pre.risk_flags if f in _SOFT_RISK_FLAGS]
    keep_full_budget = False

    if hard_flags:
        tier = "L"
        reasons.append(f"hard-risk ({','.join(hard_flags)}) → force full pipeline")
    elif (pre.explicit_small_scope and typ in ("chore", "docs")
            and clarity == "clear" and size != "L" and not non_dev):
        # Explicit-small-scope fast path (GH issue #5, item 2): the prompt
        # itself asserts a tiny bounded change ("one file", "3 lines", "only
        # .gitignore"...). This deterministic signal outranks a soft-risk
        # keyword hit — a chore/docs change explicitly this small should not
        # be kept at M by e.g. a stray "migration" match. Hard-risk still
        # dominates (checked above); size=='L' still fails safe to L below via
        # the normal branch, since it is excluded from this condition.
        tier = "S"
        reasons.append(
            f"explicit small-bounded-scope + type={typ} + clear → "
            "deterministic S fast path (overrides soft-risk)")
    elif soft_flags and not non_dev and size in ("S", "M") and clarity == "clear":
        # Soft-risk small+clear change: lighter STAGE subset (M drops the upstream
        # architect/pattern reasoning) but keep L's convergence budget (below).
        tier = "M"
        keep_full_budget = True
        reasons.append(
            f"soft-risk ({','.join(soft_flags)}) + size={size} + clear → "
            "standard tier (drops upstream reasoning, keeps L iteration budget)")
    elif non_dev:
        tier = "M"
        reasons.append("not a clear dev task → keep BA to clarify (M)")
    elif size == "L":
        tier = "L"
        reasons.append("size=L → full pipeline")
    elif clarity == "underspecified":
        tier = "L"
        reasons.append("clarity=underspecified → full pipeline")
    elif conf < thr.conf_threshold:
        tier = "L"
        reasons.append(f"confidence {conf:.2f} < {thr.conf_threshold:.2f} → full pipeline (fail-safe)")
    elif size == "S" and risk == "low" and clarity == "clear":
        tier = "S"
        reasons.append("size=S, risk=low, clarity=clear, confident → trivial tier")
    else:
        tier = "M"
        reasons.append("default standard tier")

    caps = tier_caps(tier)
    if keep_full_budget:
        # A soft-risk task runs fewer STAGES but deserves the SAME convergence
        # budget as L — the saving is the dropped upstream reasoning, not fewer
        # hotfix iterations (the channel-id fix genuinely needed all 3).
        l_caps = tier_caps("L")
        caps = {
            "iteration_cap": l_caps["iteration_cap"],
            "token_cap": max(caps["token_cap"], l_caps["token_cap"]),
        }
        reasons.append(
            f"soft-risk: iteration_cap={caps['iteration_cap']}, "
            f"token_cap={caps['token_cap']} kept at L budget")
    return Triage(
        tier=tier,
        verdict="non-dev" if non_dev else "dev",
        estimate=tier,
        dimensions=dims,
        confidence=conf,
        reasons=reasons,
        caps=caps,
        source=source,
    )


def decide(
    prompt: str, target_repo: Path | None, spec: dict | None = None,
    run_claude=None, thr: Thresholds | None = None,
) -> Triage:
    """End-to-end convenience: pre-scan → (optional LLM verdict) → classify.

    ``run_claude`` is an injected ``(prompt) -> str`` callable; pass None to
    skip the LLM and classify deterministically. Never raises."""
    thr = thr or Thresholds.from_env()
    pre = prescan(prompt, target_repo, spec)
    verdict = llm_verdict(prompt, pre, run_claude) if run_claude is not None else None
    return classify(pre, verdict, thr)


# ── Routing (tier → stage subset) ─────────────────────────────────────────

def stages_for_tier(tier: str, full_stages: list[str]) -> list[str]:
    """Narrow the fully-composed (flag-driven) stage list to the tier subset.

    Order is preserved; the NEVER_DROP core (developer/test/security/reviewer)
    always survives. L = full; M = BA + core; S = core only. This is the single
    mechanism by which triage saves money — by dropping redundant UPSTREAM
    reasoning stages, never review/test/security (RFC §Key-Finding-6)."""
    full = list(full_stages)
    if tier == "L":
        return full
    if tier == "M":
        keep = set(M_EXTRA) | set(NEVER_DROP)
    else:  # "S"
        keep = set(NEVER_DROP)
    return [s for s in full if s in keep]


# ── Upgrade ladder (underestimation recovery, RFC §Q5) ────────────────────

def next_tier(tier: str) -> str | None:
    """One step up the ladder S→M→L. None at L (already maxed). Never skips a
    step unless a hard risk flag appears (the caller handles that case)."""
    return {"S": "M", "M": "L"}.get(tier)


def caps_for_tier(tier: str) -> dict:
    return tier_caps(tier)
