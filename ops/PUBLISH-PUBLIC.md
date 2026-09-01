# Publishing to the Public Mirror

> **Status (2026-09-01): publication is LIVE again** (owner decision; it was
> paused 2026-08-21 → 2026-09-01). Development happens only in the private
> repository; the mirror receives sanitized squash releases after milestones,
> as part of the release ritual (ops/RELEASE.md). Two things still follow:
> a publish is a deliberate act behind an owner decision, not something to
> run on your own initiative — and **never `git push` to the mirror**: the
> only path is this flow, with its gates. First resumed publish: v1.2.0
> (mirror commit `63cf00f`).

`scripts/publish-public.sh` is the single supported path for refreshing the
public mirror (`github.com/Sekator778/ai-delivery`, remote `public-mirror`).
It is fail-closed, idempotent, and requires an explicit flag to do a real push.

---

## What gets excluded

The following paths are stripped from every export, regardless of which ref
you publish:

| Excluded | Reason |
|---|---|
| `STATE/` | Internal planning, decisions in Russian |
| `research/` | External whitepapers, chat exports |
| `briefs/` | Phase handovers, partially bilingual |
| `memory-bank/` | Agent memory bank (entire directory, no carve-outs) |
| `.claude/` (partial) | Operator harness config and vendored templates; kept: `.claude/agents/**`, `.claude/commands/**` |
| `bot/projects.json` | Absolute host paths and per-deployment registry |
| `*.env*` files | Secrets and credentials (exception: `*.example` files are kept) |
| `tasks/` (partial) | Task queue contents; kept: `tasks/_TEMPLATE/**`, `tasks/README.md`, each column's `.gitkeep` |

These constants are hard-coded in the script (`readonly EXCLUDE_DIRS`,
`readonly EXCLUDE_FILES`, `readonly CLAUDE_KEEP_PREFIXES`) and cannot be
overridden by an environment variable (ADR-006). Widening the export requires a
code review.

**Why `.claude/` is split rather than excluded whole** (2026-08-15). The agent
persona definitions are the product, not configuration: `dispatcher/stage_prompts.py`
dispatches subagents by the names defined in `.claude/agents/`, so a mirror
without that directory ships a pipeline whose stages cannot run — and, from
v1.0.1, a `tests/test_reviewer_lenses.py` that cannot pass. `v1.0.0` shipped in
exactly that state. Still excluded: `.claude/templates/` (vendored BMAD and
spec-kit, upstream licensing — publishing them is a separate decision) and
`.claude/settings.json` (operator harness config). The `.claude/` rule is
fail-closed: a path matching no keep-prefix is dropped, so a subdirectory added
later stays private until it is explicitly listed.

---

## Two-layer secrets/PII gate

Every run executes two independent scans over the filtered export before any
commit is built:

**Layer 1 — gitleaks**
Runs `gitleaks dir` over the materialized export with all bypass channels
disabled (`--ignore-gitleaks-allow`, `--gitleaks-ignore-path` pointed at an
empty file, hard abort if the export contains `.gitleaksignore` or
`.gitleaks.toml`). Any finding blocks the publish.

**Layer 2 — project blocklist**
Runs `grep -rIlE` over the materialized export once per pattern in
`ops/publish-blocklist.local`. Each finding is reported with its matched
pattern and file path. Any hit blocks the publish.

**Message scan**
The composed commit message is also scanned by both layers before
`git commit-tree` runs, so an internal hostname or token pasted into
`CHANGELOG.md` cannot slip through.

### One-time local setup (required before first publish)

```bash
cp ops/publish-blocklist.local.example ops/publish-blocklist.local
# Open ops/publish-blocklist.local and fill in your real patterns, e.g.:
#   192\.168\.1\.[0-9]+
#   myhost\.internal
```

`ops/publish-blocklist.local` is gitignored and must be created once per host.
The script aborts at preflight if it is absent (`exit 2`), with a message
pointing at the example file.

---

## Usage

### Default: dry run

```bash
scripts/publish-public.sh
```

Performs the full export, both scans, and commit construction, then prints:
- Which top-level paths are excluded
- Layer 1 verdict (gitleaks: N findings)
- Layer 2 verdict (blocklist: N effective patterns, M hits)
- Message scan verdict
- A diff-stat against the public mirror's current `main`
- The exact push command that would be required

No network write occurs. The public mirror is not changed.

### Specify a ref

```bash
scripts/publish-public.sh --ref v1.0.0
```

Defaults to `dev` when `--ref` is omitted.

### Ref freshness

The ref is checked against `origin` before anything is exported, and a ref that
is not in sync aborts at preflight (`exit 2`):

| State | Result |
|---|---|
| In sync with `origin/<ref>` | Proceeds, and says so |
| Behind | Refuses — publishing would ship a stale tree |
| Ahead | Refuses — those commits are not even in the private repo |
| Diverged | Refuses — no publish from here is the whole branch |
| `origin` unreachable | Refuses — freshness is unknown, not confirmed |
| No `origin` remote, or `<ref>` not on `origin` | Proceeds, and says which |

This exists because it already went wrong: public commit `19238a3` (2026-08-21)
shipped a tree from a stale local `dev` under a summary describing changes that
were not in it. Nothing warned, and the summary looked precise — it prints a
SHA, just the wrong one.

The check refuses rather than warns, which is the opposite of the choice made
in `aidup` for the same class of problem. That is deliberate: a stand running
yesterday's code beats a stand that will not start, and a human is watching the
output. Publishing inverts both — it writes irreversibly to a live public
repository, and nobody eyeballs the result.

To publish from a ref that is knowingly out of sync — re-issuing an old tag,
say:

```bash
scripts/publish-public.sh --ref v1.0.0 --allow-stale-ref
```

The override announces itself in the output. It is a flag and not an
environment variable on purpose: the remote being compared against is a
`readonly` constant, so there is no quiet way to turn the check off.

### Custom commit summary

```bash
scripts/publish-public.sh --summary "Add publish script and two-layer gate"
```

When `--summary` is not given, the first bullet line from `CHANGELOG.md`'s
`## [Unreleased]` section is used. If that section is empty, the fallback is
`Forwarded from <ref>@<sha>`.

### Real push

```bash
# Push with an explicit URL (primary mechanism):
scripts/publish-public.sh --push --push-url git@github.com:Sekator778/ai-delivery.git

# Push via the remote's own fetch URL (process-scoped, never persisted):
scripts/publish-public.sh --push --allow-temp-pushurl
```

`--push` alone exits with code 2. It requires exactly one of `--push-url` or
`--allow-temp-pushurl`. The disabled `pushurl` sentinel in `.git/config`
(`DISABLED_PUBLISH_DELIBERATELY_VIA_SCRIPT`) is never modified by the script.

### Offline self-test

```bash
scripts/publish-public.sh --self-check
```

Builds a disposable fixture repo, runs a dry-run against it, and exits 0 if
all phases complete. Contacts no remote.

### Debugging

```bash
scripts/publish-public.sh --keep-tmp
```

Keeps `TMPROOT` (the ephemeral index, export directory, gitleaks report, and
sanitized pattern list) after the run so you can inspect them.

---

## Idempotency guarantee

If the filtered export tree is byte-identical to the public mirror's current
`main` tree (compared by git tree SHA), the script prints "Nothing to publish"
and exits 0 without creating a commit or attempting a push — even when `--push`
is given. Re-running after a successful push always hits this path.

---

## Commit metadata note

The commit's author and committer identity come from the local git config
(`user.name` / `user.email`) on the host that runs the script. This is already
the identity on every existing public commit, so publishing does not expose new
personal information. Rotate the config before publishing from a different host.

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success: published, dry-run completed, nothing to publish, or self-check ok |
| 1 | Usage error (unknown flag) |
| 2 | Preflight failure: invalid ref, ref not in sync with `origin`, missing gitleaks, missing blocklist, no tag, unauthorized `--push` |
| 3 | Gate finding: secrets or PII in export or commit message |
| 4 | Push or verification failure |

---

## Rollback

The script never force-pushes and never rewrites public history. If a bad
commit was pushed, roll forward by publishing the previous tree as a new commit:

```bash
git fetch public-mirror main
OLD=<sha-before-bad-push>
BAD=$(git rev-parse FETCH_HEAD)
R=$(git commit-tree "${OLD}^{tree}" -p "$BAD" -m "Revert to $OLD: publish rollback")
git push git@github.com:Sekator778/ai-delivery.git "${R}:refs/heads/main"
```

This restores the exact previous tree, keeps history append-only, and uses the
same primitive as the script itself.
