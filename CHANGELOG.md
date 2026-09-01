# Changelog

All notable changes to ai-delivery are listed here. Dates are when the
milestone landed on the development branch (private upstream). Development
happens privately; releases are forwarded to the public mirror as sanitized
squash commits through the gated export (publication resumed 2026-09-01
after a pause since 2026-08-21).

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
the project does not yet follow strict SemVer (pre-1.0).

## [Unreleased]

Entries accumulate here until the next tag.

- **Rooms run side by side (backlog/T32).** `/room` no longer awaits the
  whole loop inside the command handler — which, with the bot processing
  updates one at a time, made the bot deaf for the minutes a room took. The
  room is a background task under a capped registry (`ROOM_MAX_CONCURRENT`,
  default 3); over the cap the bot says so; a crashed room reports to the
  chat instead of vanishing.

- **The room's cap is a permission to hire, not only a stop (backlog/T31).**
  A hire the room cannot pay for is refused before the child runs (per-option
  floor, reason in the history, the prompt names what is affordable); a room
  cut short by budget or turns delivers the files and the last specialist's
  own summary instead of "nothing collected"; and the model the conductor
  bought is the model the child runs — `run_subtask` gained a `model` pin,
  so the masked price table stops being fiction on the anthropic backend.
  Extraction now logs "N parsed, M written" into the bot log.

- **The room remembers (backlog/T30).** Every terminal room outcome now runs
  the T26 strategy extraction over its delegation history — success and
  failure branches picked by a programmatic verdict — and stores the parsed
  items under the literal scope `room`; the next room's conductor gets the
  relevant ones injected into its first prompt as PAST STRATEGIES. Delivery
  always precedes memory, a memory failure is a logged zero, and either flag
  off keeps the room byte-for-byte as it was. Specialist history entries now
  carry the specialist's final text instead of a sliced stream tail (the T29
  lesson applied to the remaining spot).

## [1.2.0] — 2026-09-01

The milestone: DeepSeek carries every mechanical stage, the stand deploys
itself safely (`restart --wait`), the functions runtime got its governance,
and the next iteration — the "specialists room" (a single entry point for
any task, not only code) — is designed, researched against working
reference implementations, and cut into briefs (backlog/T25, T26).

- **DeepSeek now carries all eight mechanical stages** (owner directive,
  2026-08-31). `DEEPSEEK_STAGES` gained `pattern-detector`, `tasks`,
  `analyze`, `edge-cases` on top of the build/verify four. Grounds: the
  live ledger showed ~92% of a task's cost sitting in anthropic stages;
  first live task after the change ran pattern-detector for $0.09.
  ba/architect stay on Opus, reviewer on Sonnet; the L-tier guard,
  per-task `model_routing` and the iteration-2 escalation still win.
- **`/task @alias` followed by a newline resolves again** — the alias
  parser split the body on the first space only, so a task text starting
  on the next line swallowed the newline into the alias token and failed
  as "unknown alias" while listing that very alias as known.
- **Functions runtime got governance and packaging** (backlog/T21, T22 —
  work landed in the sandbox repo). Promote now enforces a per-function
  contract check, the active set is bounded, retirement exists, and
  SKILL.md packaging is generated from source annotations with a sync
  test — the roadmap #1/#3 pair that was blocked on the owner's
  functions-runtime verdict (GO 2026-08-31).
- **The specialists-room iteration is designed and grounded.** Design
  note (`STATE/DESIGN-2026-08-31-front-door.md`): one entry point for any
  task, a conductor that synthesizes specialists on demand (no salaried
  roster), approve-gate generalizing the merge gate. Two reference
  implementations studied at code level (`research/*-code-study-2026-09.md`):
  AOrchestra (the conductor pattern, ~600 lines of core) and ReasoningBank
  (strategy memory distilled from successes AND failures). Briefs cut:
  T25 (conductor MVP), T26 (strategy memory over the flat store).

- **The room reads the CLI's result event instead of its raw stream**
  (backlog/T29). T28 handed the whole stream-json stdout to the decision
  parser, which takes the first balanced object — a CLI hook event, with no
  `action` field — so every conductor turn came back unparseable, including a
  perfectly good delegation on the live run. The decision now comes from the
  result event's `result` field, and cost from its `usage`, with both cache
  columns mapped to the names the pricing helper reads. A child that crashed
  before emitting a result event keeps the honest failure path.
  The lesson is the mock/reality seam: every T28 test fed clean JSON, so the
  suite was green while the live room could not read a single turn. Those tests
  stay — they cover the protocol — and new ones use the shape the CLI actually
  emits, including one that pins that the raw stream does *not* parse, so the
  extractor is not removed as noise later.
- **The built-in DeepSeek price table matched the operator's override at
  last.** atlas priced correctly through `BACKEND_PRICES_JSON`; every clone
  without it — CI included — computed on pre-17-August rates and understated
  DeepSeek spend roughly threefold, silently, because a wrong number in a
  ledger looks like a right one. Found by an arithmetic disagreement between
  the T29 brief and the code, where the brief was right.

- **The room now drives its loop past turn one** (backlog/T28). T25 shipped the
  conductor and no driver: on the live run it returned a valid delegation for
  $0.178 and then nothing happened — `room_command` ended at `run_subtask` and
  the completion callback did not know room ids, so the loop could not continue
  by construction. `dispatcher/room_driver.py` is that loop, kept pure with
  injected spawn/notify because bot.py cannot be imported in a test.
  `run_subtask` now returns `(rc, output)` — additive, with all three call
  sites audited first — because a completion callback is a notification, not a
  channel. Budget is debited in the provider's currency via
  `apply_backend_pricing`, and the conductor's own turns count: turn one alone
  was 9% of the default cap. `finish` delivers the scratch files as well as the
  summary, since "the report is ready" without the report is the same bug as
  silence. Every terminal path — budget, turn limit, unparseable conductor,
  failed specialist — sends something.
- **`web-research` specialists are refused on non-web-capable models rather
  than silently upgraded.** Server-side web search is an Anthropic API tool; on
  a DeepSeek-compatible endpoint a research specialist has no search and
  answers from memory instead of failing. Quietly moving it to a pricier
  backend would make the conductor's price table a lie, so the delegation is
  refused with the legal choice named, and the table gained a web-capable
  column so the constraint is visible while choosing.
- **A child cost with no token counts is no longer repriced to $0.00.** Found
  by the driver tests before any live run: repricing computes from tokens, so a
  result line carrying a cost but no counts booked nothing, and a room that
  debits nothing spends without limit. It now falls back to the provider's own
  figure under a `cli-fallback-no-tokens` label.

- **Strategy memory, behind `MEMORY_STRATEGY_ENABLED` (default off)**
  (backlog/T26). Finished tasks now teach the next one: a verdict on the
  outcome, a `status`/`verdict_source`/`source_query` record, and two
  extraction branches so failures produce recovery procedures rather than
  nothing. Ported from ReasoningBank read as code.
  The load-bearing detail is counter-intuitive and only visible in that code:
  retrieval matches a new request against past *requests*, so the embedded text
  is `source_query + description`, not the lesson. Embedding the lesson — the
  obvious choice — means a good strategy from a differently-worded task is
  never retrieved.
  Verdicts prefer the programmatic signal the pipeline already has; the LLM
  judge is a fallback and uses the WebArena prompt with its strictness rules,
  not the SWE-bench one whose `"success" in reply` test reads "this was not a
  success" as a success. Items are parsed into typed fields and dropped when
  incomplete; near-duplicates are skipped on write. No consolidation: the bank
  stays append-only.
  With the flag off the written record and the embedded text are exactly what
  they were, pinned by a test that checks both.

- **Room conductor MVP, behind `ROOM_ENABLED` (default off)** (backlog/T25).
  `/room <request>` hands a free-form task to a conductor that hires one
  synthesised specialist at a time — the quad ⟨instruction, context, tool
  profile, model⟩ — rather than staffing a static catalogue. Models are masked
  (`model_1..3`) behind a price table so the choice is about what the subtask is
  worth. Two read-only tool profiles; no outbound capability at all, because the
  approve-gate that would govern one does not exist yet. Hard budget cap per
  room, unlike the reference, which only ever counted.
  Three fragilities from the AOrchestra code study were deliberately not
  copied: a malformed reply is an ordinary turn rather than the end of the task;
  every turn is recorded, including refusals, so the conductor can see its own
  mistakes; and the delegation history is capped with the truncation announced.
  An unknown model is a validation error, not a $0 default.
  With the flag off the handler is never registered and the command is never
  advertised, so the bot behaves exactly as before — pinned by a test.

- **Public artifacts are English, and a gate now enforces it** (backlog/T27).
  CLAUDE.md §2 always required it; nothing checked, so Russian reached the
  public mirror on the first resumed publish. `publish-public.sh` gained a
  third gate layer — `ops/check-cyrillic.py` over the export, fail-closed,
  exit 3 — with a file allowlist where every entry states which Telegram-facing
  text it carries. The sweep left 0 violations across the 275 published files.
  Not a `grep -P` over a Cyrillic character class: PCRE is byte-oriented
  without a UTF-8 locale, so the class matches every non-ASCII character (em
  dashes, CJK, emoji — 379 false hits against 211 real ones in one file), and
  macOS grep, which is what runs on the operator's machine, has no `-P` at all. Files that do not decode
  as UTF-8 are skipped, so adding a PNG to the export cannot block a publish.
- **Two files reclassified as product surface, not documentation slips.**
  `meta/CLAUDE.md` is the orchestrator's live prompt — its Russian is trigger
  phrases it must recognise and reply templates it sends — and the silero
  README documents Russian stress-mark syntax by example. Translating either
  would have changed behaviour or destroyed the example.

- **CI: shellcheck is a blocking gate, and gitleaks now scans the whole
  history.** Both were deliberate omissions waiting on a triage that has now
  been done. shellcheck runs at `severity=warning` over every tracked script
  except vendor material in `research/external/`; `info`/`style` stay
  non-blocking because SC1091/SC2015/SC2317 dominate them here and a gate
  nobody can keep green is one everyone ignores. The six historical gitleaks
  findings turned out to be two things — a DeepSeek key rotated 2026-05-27, and
  a 40-hex git commit SHA in a URL misread as a Sourcegraph token, matched five
  times — both pinned by fingerprint in a new `.gitleaksignore` with the
  reasoning inline. History was not rewritten: a dead key and a commit hash do
  not justify force-pushing 593 commits. The value is that the next finding in
  history will be a new one.
- **Two latent bugs found by that shellcheck triage.** In `aidstack.sh`,
  `local name="$1" pidfile="$PID_DIR/$name.pid"` evaluated `$name` before the
  assignment landed, so it read the *caller's* variable — correct only because
  both call sites happen to loop over one named `name`. In
  `publish-public.sh`, `TASKS_KEEP_PATTERN` was declared and never read while
  the filter beside it repeated the literal, so editing the constant looked
  like editing the filter and changed nothing.
- **`.github/` is now published to the mirror** (`PUBLIC_TOPLEVEL`). Its only
  file is the CI workflow: no secrets, and no operator data beyond the machine
  nickname already public through `ops/atlas/`. Pinned by a test, mirroring the
  one that asserts an unlisted directory never ships.

- **`aidstack.sh restart` — the command that actually deploys** (backlog/T24).
  `up` is idempotent and never touches a live daemon, so on a running stand it
  pulled new code onto disk and left the daemons executing the modules they
  imported at start — while every stage, a fresh process, read the new code, as
  did the persona and prompt files. Orchestrator and stages on different
  versions, and `up` reported success. `restart` stops, pulls and starts, so
  the running processes execute what was pulled; `up` now warns when it moved
  the checkout under live daemons.
- **`restart` and `down` refuse while a task runner is alive.** Both kill work
  in flight — `down` also sweeps orphaned `claude` children — and a stage killed
  halfway is a paid Claude call thrown away. `--wait` waits for the current task
  (`AIDSTACK_WAIT_TIMEOUT`, default 3600s) and refuses on timeout rather than
  falling through to a kill; `--force` overrides and says so. Liveness comes
  from the new `dispatcher/runner_liveness.py`, extracted from `watcher.py` so
  both sides share one definition: the check matches the process command line
  against the runner script and task id, because a pidfile outlives its process
  and pids get reused.

- **Publishing refuses a source ref that is not in sync with `origin`**
  (backlog/T23). `publish-public.sh` exported from the local ref with no
  freshness check, which is how public commit `19238a3` shipped a stale tree
  under a summary describing changes that were not in it — the summary looked
  precise because it prints a SHA, just the wrong one. Preflight now compares
  `--ref` against `origin` and aborts (`exit 2`) when it is behind, ahead or
  diverged, and also when `origin` cannot be reached: unknown freshness is not
  confirmed freshness. No `origin`, or a ref absent from it, proceeds — there
  is nothing to be stale against — but says so, because "the check did not
  apply" must not look like "the check passed". `--allow-stale-ref` overrides
  and announces itself; the remote compared against is a `readonly` constant,
  so there is no silent way off. The script already re-fetched the mirror tip
  before building and verified the tip after pushing; this applies the same
  distrust of local state to the source.
- **`die()` no longer prints its exit code as part of the message.** It
  interpolated `"$*"`, so all 19 `die "..." N` call sites emitted a stray
  ` N` at the end of the text. Invisible on one-line messages; on a multi-line
  one the digit lands under the remediation hint and reads like part of it.

- **Triage: `permission` needs an auth neighbour** (backlog/T19). Fifth word of
  the same class, after `secret`, `session`, the payment words and `token`.
  This repo lives inside Claude Code, where permission is harness vocabulary,
  not authentication — and `auth` is a HARD flag, so one match forces tier=L on
  its own: every stage on Anthropic, L-sized caps. Measured before the change:
  **seven false positives out of eight**, including `permissions: contents:
  read`, a line from this project's own CI workflow.

  `rbac` and `acl` still match bare. `check`/`checks` is deliberately *not* an
  accepted neighbour — "permission check" is as much harness vocabulary as auth
  vocabulary here, and under-matching a rare phrase beats keeping the false
  positive. `privilege escalation` is **added**, not preserved: it did not
  match at all before, there being no `privilege` in the alternation — the
  brief assumed otherwise.

  The four earlier narrowings were pinned by nothing but comments in the
  regex, so re-widening any of them would have gone unnoticed.
  `RiskWordNarrowingTests` now pins all five, in both directions.

- **Memory store: the tmp residue is gone** (backlog/T20). 21 `task_lesson`
  points scoped to `$TMPDIR` fixtures — written by runner-level tests before
  the T02 guard existed — diluted every recall, because the scoped half of
  `recall()` filters by `target_repo` and these are targets that no longer
  exist. Payload export 810 → 789 via a new
  `qdrant-memory.py purge-ephemeral`, dry-run by default.

  **21, not the ~27 the brief estimated.** The brief asked for the list to be
  read before writing, and reading it is what produced the difference: six
  further points carry a temp path in their *text* but are `session_stop` /
  `subagent_stop` summaries — real operator hand-offs quoting a path in
  passing. Selection is by `target_repo` only.

  Found on the way: `_is_ephemeral_target` was **host-dependent**. Built on
  `tempfile.gettempdir()`, it answers "is this ephemeral *here*", while the
  store is portable — the JSONL travels in git and is read from Linux, but the
  points were written on macOS, whose `$TMPDIR` is `/var/folders/<xx>/<yyy>/T/`.
  The same record was garbage on one machine and legitimate on another, so a
  purge run from the wrong host silently found nothing. The predicate now
  recognises that shape regardless of host, which also makes the production
  write-back guard strictly more correct.

  The live flat store is gitignored and lives on the host; the purge has to be
  run there too, or recall keeps serving them.

## [1.1.0] — 2026-08-28

- **Semantic recall can now run without Qdrant** (`MEMORY_FLAT_ENABLED`, off
  by default). The collection held 3.16 MB of vectors in 600 MB on disk, served
  by two always-on services, and 787 of its 809 points are frozen prose from
  hooks retired in August. `dispatcher/memory_flat.py` does the same ranking
  over a JSONL file: normalise once on load, cosine scan, the scoped half of
  recall becomes a field comparison instead of a payload filter. A stdlib scan
  of the whole collection takes about 70 ms — against three recalls per task
  and stages measured in minutes, that is noise. (The ROADMAP previously said
  "microseconds", which was wrong.)
- **Starting the stand is the deploy.** `aidup` now takes the newest commit of
  the branch the checkout is on, then starts; nothing new, or anything in the
  way, and it starts what is already there. No scheduler, no agent, no
  background process.

  The update never blocks the start: a dirty tree, unpushed local commits,
  diverged history, a detached HEAD or no network are each a warning, not a
  failure. A stand that refuses to come up because git had an opinion is worse
  than one running last week's code — and unlike an unattended loop, a human is
  reading the output. Every path returns 0, and six of the nine tests assert
  exactly that.

  It follows whatever branch is checked out, so no branch name lives in two
  places. `AIDUP_PULL=0 aidup` skips it; `aidstack.sh pull` runs just the
  update.

  This replaces the polling agent added earlier the same day and removed before
  it ever ran unattended. That version worked — 7 ms per idle cycle, no
  resident process — but it needed an in-flight marker, a failure counter with
  an attempt limit, a live-runner deferral and a stack-is-up precondition, all
  of which existed only because no human was present. With a human running the
  command, ~300 lines and 15 tests collapse to ~45 lines and 9.


- **`master` is the branch things start from, not an archive of releases.**
  Owner's call. Development still happens on `dev` and every PR is still based
  there; what changed is `master`'s job. It is the repository's GitHub default,
  so it is what a fresh clone gets, what a new harness session is cut from, and
  what anything triggered against this repo runs.

  Which gives the rule the rest follows from: `master` must not go stale. A
  trigger branch that lags behind `dev` runs old code and reports on it
  confidently. `dev` is merged into `master` as soon as work has landed and CI
  is green — not only at a version tag. Fast-forward when it can be, an
  ordinary merge when it cannot; "it would not fast-forward" is a reason to
  find out what landed out of band, not a reason to leave `master` behind.

  The earlier recommendation to switch the GitHub default to `dev` is dropped:
  the default stays `master` precisely because that is what triggers run from.
  The cost is accepted — GitHub preselects `master` as a new PR's base and it
  has to be retargeted by hand.

  Tagging a version is now a separate, smaller section rather than the only
  path to `master`. `ops/RELEASE.md` splits accordingly, and its post-check
  becomes `git rev-list --count master..dev` = 0, which catches staleness and
  not just a stranded tag. `CONTRIBUTING.md` "Branch model" and `CLAUDE.md` §1
  are rewritten to match.

- **Triage stops reading "token" as authentication.** `auth` is a hard risk
  flag: one match forces tier L whatever size and clarity say. It carried a bare
  `token` — and in this project a token is the unit of the budget, not a
  credential. `token_cap`, "token budget", "input/output tokens", "500k tokens",
  the tokenizer; one whole brief is about the token cap. The line "tokens, not
  dollars, are the budget unit on a subscription", from our own test docstring,
  came back as `risk=auth`.

  `token` now needs an auth-side neighbour — a qualifier in front (access,
  refresh, bearer, auth, session, id, csrf, api, reset, bot, personal access, …)
  or an auth action behind it (revocation, rotate, expiry, validation, leak,
  scope, introspection, …). The unambiguous words still match bare, so "JWT",
  "OAuth", "credential", "password" and "login" carry most real auth text
  anyway. Checked both directions: ten lines of this project's budget vocabulary
  no longer flag, twelve lines of real credential work still do.

  Fourth instance of one shape, after `secret`, `session` and the payment words:
  a term that means risk in the domain and ordinary work in this repository's
  own vocabulary. Policy is untouched — hard-risk dominance, the L guard and the
  iteration-2 escalation were all bought with incidents.

- **Retiring Qdrant, the rest of the way.** The flat store landed with the
  runner switched over and everything around it still Qdrant-shaped. Now:
  `ops/atlas/aidstack.sh` reads `MEMORY_FLAT_ENABLED` from `bot/.env` and, when
  it is on, neither starts the container nor waits on `:6333` — the next
  `aidup` no longer resurrects it — while `aidstatus` reports the store's point
  count, size and age, and says MISSING or EMPTY when it should. With the flag
  off the script behaves exactly as before, because the rollback has to stay
  real.

  The bot's `/memo` and `/recall` went through `MEMO_QDRANT_URL` directly and
  answered `❌ Qdrant: …` with the container stopped. They now call
  `memory_inject.remember()` / `search_text()`, so they follow the flag like
  every other reader. That also removes a bug nobody had noticed: the bot
  embedded with FastEmbed `multilingual-e5-large` while the runner embeds with
  TEI `bge-m3` — two models writing vectors into one collection, where
  cosine distance between them means nothing.

  And `dump --with-vectors`, the documented migration path, did not work: it
  scrolled the vectors and then dropped them on the floor, writing the
  payload-only filename. It writes `meta_agent_mem.vectors.jsonl` now, in the
  shape `memory_flat.load()` reads, with vectorless points reported rather than
  silently emitted. The live store on the host was produced by a one-off script
  during the migration; this is the reproducible path, pinned by a test that
  dumps and then loads the result.

  Git policy for the store is now written down (ADR `flat-store-git-policy`):
  the payload dump is committed, the vectors file is gitignored — and the
  consequence, that the live store is not backed up by git, is stated where an
  operator will read it instead of discovered after a disk dies.

- **Stage children can be confined to a network allowlist**
  (`EGRESS_SCOPING_ENABLED`, off by default). A stage runs
  `claude --dangerously-skip-permissions` over a repository whose *content* is
  the attack surface, and its Bash tool could reach any host on the internet.
  The env allowlist closed "leak the key"; it never closed "leak through a
  request".

  The CLI already ships the mechanism — a domain allowlist enforced by a local
  proxy with a kernel backstop that drops non-loopback traffic at the socket
  layer — so this configures it rather than reimplementing it, in the
  `settings.json` the pipeline already rewrites on every run. Four of the
  settings are load-bearing and are commented as such where they live: the
  sandbox fails closed instead of warning and running unsandboxed; the agent
  cannot lift it through `dangerouslyDisableSandbox`; filesystem isolation is
  deliberately off because a stage must write its worktree; and the target
  repo's own settings cannot widen the allowlist.

  The allowlist covers the model endpoints, GitHub, and the package registries
  a stage needs to build its target — that last group is the honest weak spot,
  since a publish to a registry is a channel it cannot close. It still narrows
  "the whole internet" to "vendors plus registries". Extend per host with
  `EGRESS_EXTRA_DOMAINS`.

- **A provider can have more than one key now** (`bot/providers.json`, absent by
  default). Until now `_subagent_env` read a single `DEEPSEEK_API_KEY` /
  `GLM_API_KEY` from the environment, so a personal key and a work key could not
  coexist, and the ledger recorded which *backend* paid but never which *key* —
  spend could not be split per quota.

  A registry names profiles; each points at an environment variable or a file,
  never at a literal key, because a JSON full of keys is just a second `.env`
  without the protection the first one has. Selection is per task (a
  `model_routing` value may carry `"deepseek:alt"`) or per session (`/backend
  deepseek:alt` in Telegram, stored durably and stamped into new specs as
  `provider_profile`), falling back to `defaults.<backend>`. **Without a
  registry every path behaves exactly as before** — that regression is pinned by
  a test rather than asserted.

  The profile name is attribution, and it travels: `state.json`, a new
  `profile` column in the cost ledger (added by an idempotent migration, so a
  database written before this keeps working), and a "By key profile" slice in
  `ops/cost-report.py`. The key value does not travel: the parent resolves it
  and writes only `ANTHROPIC_AUTH_TOKEN` into the child, the profile's own
  variable is never added to the child-env allowlist, and a stage running on a
  profile no longer sees the provider's default key at all — which is slightly
  *stricter* than before.

  A profile whose variable resolves to nothing degrades exactly like a missing
  global key: warn, fall back to anthropic. A profile is dropped when the stage
  escalates to another backend — a deepseek profile means nothing to anthropic.

- **The branch model is written down, and a release has a checklist.** The
  model in the owner's head — `dev` is current, `master` is released versions,
  work branches die with their PR — was not in any document, and the repository
  had drifted from it: `master` sat 133 commits behind `dev`, and the `v1.0.1`
  and `v1.0.2` tags were cut on `dev` and never reached `master`, so "master =
  releases" had quietly stopped being true two releases ago.

  `CONTRIBUTING.md` now carries a "Branch model" section (branch roles, PRs
  based on `dev`, delete-after-merge with the `merge-base --is-ancestor` proof
  before deleting anything by hand, merge commits into `dev` and fast-forward
  only into `master`, hotfixes from `master` only when a release is broken), and
  `ops/RELEASE.md` is the one-page release checklist. `CLAUDE.md` §1 points at
  both in one line instead of repeating them. The post-release check in that
  checklist is `git merge-base --is-ancestor vX.Y.Z master` — the assertion that
  would have caught the drift the first time it happened.

  CI now also runs on pushes to `master`: a release fast-forward should not be
  the one merge nobody checked.

- **Triage stops reading this project's own vocabulary as payment risk.** The
  `payment` pattern is a HARD risk flag — one match forces tier L whatever the
  size and clarity say — and it carried bare `checkout`, `subscription`, `ledger`
  and `charge`. Those are ordinary words here: `git checkout -b`, the Anthropic
  subscription window, the cost ledger the pipeline writes per stage, "the CLI
  charges every session at Anthropic rates". Any task mentioning one of them
  risked the full pipeline on the expensive backend, which is how a single
  "session" killed a task on its cost cap in August.

  Each of the four now needs a payment-side qualifier (`checkout page/flow/cart`,
  `subscription billing/fee/renewal`, `general ledger`, `ledger entry`, `charge
  the card`, `chargeback`). The unambiguous terms — payment, billing, invoice,
  stripe, paypal, braintree, refund, payout — still match on their own, and real
  phrasing like "Stripe checkout flow" or "subscription billing" keeps matching
  through them anyway. Same narrowing, same reason as `secret` in June and
  `session` before this. Policy is untouched: hard-risk dominance, the L-guard
  and the iteration-2 escalation are incident-bought and stay.

  Checked by hand both ways — ten strings from this project's own vocabulary no
  longer flag, ten real payment strings still do.

- **A clarification pause can now time out into the BA's own defaults**
  (`CLARIFY_DEADMAN_HOURS`, off by default). When the BA leaves
  `[NEEDS CLARIFICATION]` markers, the task parks in `awaiting-input/` and waits
  for a Telegram reply — with no timeout at all. On 2026-08-17 two live tasks
  stood in that pause for about three hours because the questions went unnoticed,
  and the answers they eventually got were the defaults the BA had itself
  proposed: the spec-kit contract requires every marker to carry one.

  With the dial set, the watcher resumes such a task by writing those defaults
  into the same `clarifications.md` an operator's reply would have produced —
  the answer text points the BA back at the default it recorded and asks it to
  state the choice explicitly, rather than inventing an answer here. The worklog
  and Telegram both say the resume was automatic and unconfirmed.

  Bounded on purpose. Exactly one automatic resume per task, counted in state and
  carried across re-ingest, so a second clarify pause — the BA asking again after
  seeing the defaults — waits for a human. The age is measured from an explicit
  pause stamp, never from the state file's mtime, which re-ingest rewrites. A
  task parked with no readable stamp and no recoverable questions is left alone.
  At the default of 0 hours the sweep returns before touching anything.

  A reply that arrives after an automatic resume was already rejected (the task
  is no longer in `awaiting-input/`), but the rejection called the task "closed"
  when it was in fact running; it now says the task continued on defaults and
  points at `/requeue`.

- **Triage no longer reads every "session" as an auth risk.** The risk scanner's
  `auth` alternation carried a bare `session`, and a hard risk flag *dominates*:
  it forces tier L regardless of how small or clear the change is. On the task
  behind issue #20 — killing orphaned `claude` child processes — the single
  qualifying word in the whole prompt was "own session per child", in the sense
  of a process session. That one word bought the task the full pipeline with
  every stage on the expensive backend, and it stopped on its cost cap.

  `session` now has to be qualified to count: `session cookie`, `session
  fixation`, `session hijacking`, `session replay`. `session token` and `session
  credential` still match through the neighbor words they carry, and every other
  auth term (`login`, `oauth`, `sso`, `jwt`, `rbac`, …) is untouched. This is the
  same narrowing, for the same reason, as dropping the bare `secret` from the
  crypto pattern in June.

  Checked by hand both ways: seven harmless uses of session (process sessions,
  tmux, a rate-limit window, a stored session id) no longer flag, and eight real
  auth strings still do. What is deliberately NOT changed is the policy around
  the scanner — hard-risk dominance, the L-guard and the iteration-2 escalation
  are incident-bought and stay exactly as they are. This is scanner precision,
  not a relaxed guard.

- **`ops/cost-report.py` — the ledger finally has a reader.** Every finished
  stage has been appending an honest cost row to the SQLite ledger for a while,
  and `dispatcher/cost_ledger.py` says out loud that callers query it "via
  sqlite3 CLI or a future /cost-report command". There was no such command, so
  the questions the data exists to settle — which stage is the money, what a
  task actually cost, what DeepSeek's off-peak half-rate would mean — were
  answered by impression.

  Three slices over a `--since`/`--until` window (7 days by default): per stage,
  with each stage's share of the *average task* alongside its share of the
  window, because those differ whenever tasks differ in size and it is the
  per-task share that answers "is the reviewer 40% of a task?"; per task, with
  its triage tier when the task directory is still around; and the recomputed
  (`computed:<model>`) rows split into peak and off-peak, since the price
  override deliberately holds peak rates as an upper bound and the off-peak
  bucket is therefore a known overstatement.

  Markdown tables on stdout (meant to be pasted into a note), `--csv DIR` for
  spreadsheets, `--task <id>` for one task stage by stage. stdlib only. The
  database is opened `file:...?mode=ro`, so sqlite — not a code review —
  guarantees the tool never writes to the ledger.

- **A budget cap between stages now parks the task instead of failing it.** The
  operator gate that turns a cap stop into `awaiting-input/` plus a
  [Continue]/[Discard] prompt existed only on the post-pipeline path (after the
  reviewer's verdict) and in the watcher. The three checks that fire *between*
  stages — the token cap, the per-stage cost cap, and the pair check after the
  parallel tester+security run — wrote `stage="failed"`, announced
  `FAILED: cost cap hit…` and returned rc=5, so the task was carried off to
  `failed/`. Caps are tuned empirically and will fire; every such stop is a
  designed gate, and `failed` misreported it. Reviving one of those tasks meant
  editing `state.json` by hand instead of one tap on Continue.

  All three now go through a single branch, `_cap_stop()`, which writes the
  worklog and history breadcrumbs and then calls `budget_gate.park()`. The
  park's `budget_stop` notification *replaces* the FAILED message rather than
  adding to it, so a cap stop still emits exactly one terminal message.
  `ARCHITECTURE.md` §5 already described this behavior — the code now matches
  the document.

  Two consequences worth naming. The parallel pair parks once: its two stages
  run concurrently, so whichever hit the cap first used to send its own FAILED
  line — a second message for one stop — and, now that parking moves the task
  directory, would have pulled it out from under a stage still writing to it;
  the children record the stop and the single park happens after both have
  joined. And `token_cap` becomes its own stop reason instead of reporting as
  `cost_cap`, so the prompt names the cap that actually stopped the run.

  The rc=5 pass-through in `_terminate_pipeline` is unchanged: the graceful
  handoff still must not reclassify a budget abort, and rc=5 now short-circuits
  for the same reason the clarification pause does — the task has already moved
  itself. Continue needed no change (it bumps the caps in `spec.json` and
  bounces the task to `inbox/`, which never depended on a reviewer verdict or an
  open PR), and the watcher already refuses to respawn `awaiting-input`.
- **Public mirror history reset to a sanitized baseline.** The mirror's
  previous 13-commit history is replaced by a single baseline commit built
  by the gated export from the current tree; the old tags were dropped.
  Early commits predated the export gate and carried internal material
  that was stripped from HEAD long ago but stayed reachable in history,
  and release commits carried cross-project references; commit authorship
  moved to the GitHub noreply address. Content matches the v1.0.2 release
  plus the sanitization fixes; granular per-change history resumes from
  the new baseline. The publish blocklist and the pre-push hook now derive
  personal markers instead of hardcoding them, so the export can never
  carry the strings the gate exists to block.

- **CI added (`.github/workflows/ci.yml`).** The repo had no workflows at all —
  `git log --all -- .github` is empty and the GitHub API listed only the
  built-in dependency-graph job — so a pull request reported no checks and a
  green PR meant nothing had been run. Three jobs: the suite under
  `unittest discover` on Python 3.10 (the documented floor) and 3.12 (what
  atlas runs); `bash -n` over every tracked shell script; and gitleaks over the
  working tree plus the commits the change adds. Verified before landing: the
  suite is green on 3.10, 3.11, 3.12 and 3.13 locally, `bash -n` passes on all
  20 tracked scripts, and both gitleaks passes are clean.

  Scope is deliberately narrow and stated in the file: no build or deploy step
  (there is no artifact, and delivery is the pipeline's Tester stage plus the
  operator's smoke gate), and no full-history secret scan — history carries six
  findings from before the 2026-05-27 key rotation, so scanning it would be red
  forever and train everyone to ignore the job. shellcheck is left out for the
  same reason until its findings are triaged. Actions are pinned by major tag,
  gitleaks by version *and* sha256; `permissions: contents: read`;
  `concurrency` cancels superseded runs because Actions minutes are billed on a
  private repo. `fetch-depth: 0` is a hard requirement, not a nicety —
  `publish-public.sh` refuses to run without a reachable tag, so the publish
  tests go red on a shallow clone.

  `.github/` is **not** added to `PUBLIC_TOPLEVEL`, so the workflow stays
  private: a new top-level directory is a deliberate decision, which is what
  the fail-closed filter is for. The dry run now prints it under
  `NEW TOP-LEVEL PATH — not published`, which is the mechanism working.

- **Publish filter is fail-closed at the top level.** `scripts/publish-public.sh`
  gains `readonly PUBLIC_TOPLEVEL` — an allowlist derived from what the
  `--ref dev` export actually contained on 2026-08-20 — and drops any top-level
  path absent from it. Previously the top level was a blacklist
  (`EXCLUDE_DIRS`), so a new directory nobody thought to list was published by
  default and neither scan objected: gitleaks and the project blocklist catch
  secrets, not internal prose. `EXCLUDE_DIRS` is kept as a second, redundant
  layer. Drops are announced per path under a `NEW TOP-LEVEL PATH — not
  published` heading, and a clean run says so explicitly. The exported file
  list is unchanged (214 files, verified by diffing the dry-run export before
  and after). `tests/test_publish_public.py` gains a case asserting an unlisted
  top-level directory never reaches the export; the three gate tests now plant
  their leak fixtures under an allowlisted directory, since a root-level one
  would now be dropped before either scan could see it.

- **`aidstack.sh` reports the TEI embedding server.** `dispatcher/memory_inject.py`
  needs Qdrant *and* a text-embeddings server, but only the first is in the
  compose file; the second is a launchd agent owned by another project with
  `RunAtLoad=false`, so it does not survive a reboot. Because memory degrades
  to a no-op by contract, a dead TEI was completely silent — the stack reported
  healthy while recall injected nothing and write-back stored nothing. `up` and
  `status` now probe `MEMORY_TEI_URL/info` (TEI serves no `/health`) and warn,
  naming the consequence and how to start it; `up` still succeeds with TEI
  down. Both URLs are resolved once, from the environment then `bot/.env` then
  the module's defaults, so no port is hardcoded twice. Documented in
  `ops/atlas/README.md`.

- **A test run can no longer write into a live memory store.**
  `memory_inject.write_back` refuses any `target_repo` under the system temp
  directory. A dump of the live collection held 22 typed `task_lesson` points,
  **21** of them scoped to `$TMPDIR` fixtures left by runner-level tests that
  reached pipeline completion while Qdrant happened to be listening — so the
  scoped half of `recall()`, the whole point of roadmap #0, had effectively
  never held real data. This is a deliberate production behavior change: a
  lesson scoped to a directory that disappears at process exit can never be
  recalled, so writing it is pure dilution. Worktrees are unaffected
  (write-back receives `spec.json`'s `target_repo`, not the `/tmp/ai-delivery-wt`
  checkout). `tests/__init__.py` additionally defaults the memory env off, but
  is documented as *not* load-bearing: `unittest discover -s tests` never
  imports it. Proven empirically — a full suite run against a listening stub on
  `:6333`/`:8087` now makes zero requests.

- **`tasks/*/…` ignore rules cover files, not just directories.** The bucket
  patterns ended in `/`, which matches directories only, so a
  `<slug>.reason.txt` written straight into a bucket was tracked — and a
  failure reason routinely quotes a host path or a target repo. The two
  already-tracked reason files were reviewed (both clean) and left tracked; see
  the PR for why. The task-system skeleton (`_TEMPLATE/**`, `README.md`, every
  bucket's `.gitkeep`) stays tracked and still reaches the mirror.

- **`CLAUDE.md` §1 rewritten to describe the git layout that exists.** It
  documented remotes `private`/`public`, a `public-main` branch, and a
  `.git/config` push refspec — none of which have existed since 2026-08-12. An
  agent following it literally would try `git push public`, fail, and start
  "fixing" the deliberately disabled `pushurl`. It now describes `origin` /
  `public-mirror`, the disabled push URL, and `scripts/publish-public.sh`, and
  states how the granular-forward history policy is expressed in the script
  (one run per change with `--summary`). `STATE/OPEN-SOURCE-USAGE.md` was
  rewritten to match, and the `two-remote-oss-mirror` ADR carries a revision
  note rather than being rewritten.

- **The pre-push hook is described as opt-in, and now guards the current
  layout.** `CLAUDE.md` asserted a hook "is installed"; hooks live in `.git/`
  and never travel with a clone. §5 gains a fresh-clone checklist
  (`link-agent-memory.sh` + `install-git-hooks.sh`) and states that the real
  guards are the disabled `pushurl` and the gate inside `publish-public.sh`
  (NFR-007), not the hook. The installer is now idempotent and refuses to
  overwrite a hook it did not install (`--force` to override, keeping a
  backup). The hook itself was stale — it guarded a remote named `public` and a
  `public-main` branch, neither of which exists — and now blocks branch/tag
  pushes at the mirror while letting through the bare-SHA shape
  `publish-public.sh` produces, and scans added lines for secret shapes on
  every remote.

- **`memory-bank/ai-delivery/` seeded.** The per-project knowledge base has
  been specified since May and was empty, while this repo runs the pipeline
  against itself and had every run rediscover its own conventions. Adds
  `index.md`, `architecture.md`, `conventions.md` and `highload-notes.md`,
  each grounded in a named decision, review, or failed run, and deliberately
  not duplicating `docs/CALL-TREE.md` or `ARCHITECTURE.md`.

- **ARCHITECTURE.md rewritten as the intent-level view.** The 874-line
  end-of-May snapshot (dead hooks described as live, pre-move `cwd`,
  deleted personas, WSL2-era deploy) is replaced by a short document of
  design tenets and pointers: mechanical topology lives in
  docs/CALL-TREE.md (suite-gated), origins in docs/PROVENANCE.md. Stale
  §-references in CLAUDE.md, CONTRIBUTING.md and the meta personas
  updated.

- **`backlog/` added to the publish exclusion list.** A new private
  directory of work briefs for operator-driven Claude Code sessions
  (findings about this deployment, host paths, doc-vs-reality drift).
  Narrows what the mirror receives; nothing previously published is
  affected. Note the underlying weakness this had to work around: the
  top-level filter in `scripts/publish-public.sh` was a blacklist, so any
  *other* new top-level directory was published by default — unlike
  `.claude/`, which is fail-closed. Tracked as `backlog/T03`, and fixed in the
  entry above.

- **Dead mem0 lifecycle hooks removed.** The four hooks in
  `.claude/settings.json` (`inject_from_mem0` + three `capture_*`) and the
  whole `dispatcher/hooks/` directory are gone. They had been non-functional
  for months (system `python3` without `fastembed`, error swallowed), stages
  stopped seeing this repo's settings after the stage-`cwd` move, and the
  inject half was reimplemented properly in the runner
  (`dispatcher/memory_inject.py`). Legacy points they once wrote stay in
  Qdrant and are still served by the runner's global recall; the bot's
  `/memo`/`/recall` path is self-contained and unaffected. Bot `/help`
  memory section rewritten to describe the runner-side memory instead of
  the dead hooks; CALL-TREE §Hooks updated (facts block regenerated).

- **Task-scoped memory for pipeline stages (roadmap #0) — recall + typed
  write-back, in the runner.** The ba/architect/developer prompts have long
  carried an `<injected-memory>` slot that a UserPromptSubmit hook was
  supposed to fill; that path was dead (system python without `fastembed`,
  error swallowed, and since the stage-`cwd` move this repo's hooks never
  fire for stages at all). `dispatcher/memory_inject.py` moves both halves
  into the runner, on the live local services (TEI bge-m3 + Qdrant, stdlib
  HTTP, no new dependencies):

  - INJECT: before the subprocess is spawned, the literal `(none)` slot is
    replaced with top-K recalled records — points scoped to the target repo
    first (typed), then global semantic hits from the 794 legacy points.
    Failure contract: any infra problem degrades to the unchanged prompt; a
    stage never blocks on memory. Each injection leaves a worklog line.
  - WRITE-BACK: at pipeline completion one TYPED `task_lesson` point is
    written ({kind, target_repo, task_id, tier, stop_reason, pr_url, text})
    — typed metadata is what makes the scoped recall possible — with a
    per-target cap + oldest-retire dilution guard (drift-paper lesson).

  Env: MEMORY_INJECT_ENABLED / MEMORY_WRITEBACK_ENABLED /
  MEMORY_INJECT_STAGES / MEMORY_TOP_K / MEMORY_MIN_SCORE /
  MEMORY_TARGET_CAP / MEMORY_TEI_URL / MEMORY_QDRANT_URL /
  MEMORY_COLLECTION. The live canary caught a real bug before the smoke did
  (Qdrant upsert is PUT, not POST — the degradation contract held). Full
  sandbox smoke: recall injected into all three opted-in stages, task
  approved, first genuine task_lesson recorded.

- **The three VoltAgent pipeline personas no longer carry upstream's dead
  machinery.** Second cleanup pass on `backend-developer`, `test-automator`
  and `security-auditor` (the first pass fixed only backend-developer's
  headline defects): removed the "Query context manager" step
  (context-manager is the Discovery stage's own persona here, not a live
  service), the `requesting_agent` JSON protocol blocks, the canned delivery
  notifications with fabricated statistics — an open invitation for a stage
  to invent results — and the "Integration with other agents" lists naming
  ~8 agents this roster does not have. Replaced with target-repo-conventions
  openings and honest-reporting instructions; each file carries a provenance
  comment, `UPSTREAM` records the patch, and
  `tests/test_prompt_placeholders.py` pins the removals so a re-vendor
  cannot silently restore them. The agents README header also caught up with
  reality: personas travel to stages via `--agents` since the stage-`cwd`
  move, not "in future".

- **Non-anthropic stage costs are now honest (DeepSeek was inflated ~22×).**
  The claude CLI prices every session at Anthropic rates no matter what
  `ANTHROPIC_BASE_URL` points to: a real DeepSeek tester stage cost $0.05
  (provider balance $2.44 → $2.39) while $1.12 was recorded — and that figure
  fed `cost_cap_usd`, so a task could be parked having spent kopecks. This is
  step 1 of the DeepSeek-first plan (STATE/PLAN-2026-08-15): flipping stages
  onto DeepSeek before fixing this would have made the cost cap fire ~22×
  early and any backend comparison meaningless.

  `backend_routing.apply_backend_pricing` recomputes the stage cost from the
  token counts the CLI does report truthfully × a provider price table
  (deepseek-v4-pro / v4-flash rates verified 2026-07-25; DeepSeek announces
  peak/off-peak from 2026-08-16, so prices are data — override via
  `BACKEND_PRICES_JSON` — and every figure carries a `cost_source` label:
  `cli`, `computed:<model>`, or `cli-no-price-table:<backend>` for glm, whose
  inflation stays visible in the data instead of silently wrong).
  `_parse_cost` now also extracts cache-read/-creation tokens — a live stage
  showed input_tokens=14 against output=5189 with the bulk in cache reads, so
  a recompute without them undercounts. Applied at the one point where cost
  enters the stage artifact, so `_read_stage_cost_usd`, the cost cap and
  resume-carry all get the corrected figure with no further changes; the
  CLI's number survives as `cli_reported_cost_usd` for comparability.

  This also un-deadens `cost_ledger` (it had zero callers since it was
  written): every finished stage now appends a durable row — task, stage,
  backend, honest cost, tokens incl. cache, source, session — at the same
  write point as the artifact, which is the measurement base for plan step 4
  (deciding which stages earn anthropic permanently).

- **The architecture map is now a checked artifact, not prose on trust.**
  [docs/CALL-TREE.md](docs/CALL-TREE.md) records who spawns whom — from
  `aidstack.sh` through the three daemons down to the `claude -p` → Agent tool
  → persona dispatch — every node with its owning module and the reason it
  exists. It ends in a fact block that
  [ops/check-arch-map.py](ops/check-arch-map.py) generates from the code
  itself: subprocess spawn sites, dispatcher-sibling imports (nested/lazy
  included), `STAGE_AGENT_MAP` next to the `subagent_type` lines actually
  hardwired in the prompt text (the two disagree on the reviewer stage, by
  design — now visibly), hook wiring, personas on disk. `--check` diffs the
  block against the code and `tests/test_arch_map.py` runs it in the suite, so
  a commit that changes the topology fails until it also runs `--update` —
  which puts the author inside the document, next to the prose their change
  may have falsified. This kills the ARCHITECTURE.md failure mode: the same
  day the stage-`cwd` move landed, five of its `cwd` descriptions were already
  wrong. [docs/PROVENANCE.md](docs/PROVENANCE.md) consolidates the mechanism
  provenance (mechanism → source → verdict) that lived in ~15 code comments
  and internal research notes.

  Following the invariant-not-wording rule the same session established, the
  phrase-presence prompt tests (ADR "Prevents" field, BA Theater Check, the
  target-CLAUDE.md wording checks) were removed from
  `tests/test_prompt_placeholders.py`: they pinned exact prompt wording and
  were rewritten twice in one session. What remains is structural — placeholder
  coverage, the no-relative-template-citation guard, the vendored-regression
  negatives — and the instructions-actually-arrive invariant belongs to the
  sandbox smoke run (`SANDBOX_CANARY`), which is part of the contract for
  runner changes. Suite: 689 → 684 tests, same 2 known failures.

- **Stages no longer inherit the operator's personal `~/.claude`.** Moving each
  stage's `cwd` into the target project fixed *project* instructions leaking
  across repositories; it did nothing about the *user* level, which applies to
  every claude session on the machine regardless of working directory. Every
  stage of every task was running with the operator's interactive setup: a
  `Stop` hook playing a desktop sound, `effortLevel: xhigh` layered on top of
  the pipeline's own tier routing, and — the one that matters —
  `agent-limit.sh` on `PreToolUse`/`SubagentStart`, which caps concurrent
  subagents at 3 in "ONE GLOBAL bucket per machine (NOT per session_id)" and
  denies the fourth. The Reviewer dispatches three lenses in parallel, tester
  and security run as a pair, and the operator's own interactive session counts
  into the same bucket — so a lens could be silently denied and the review would
  come back clean with a hole in it.

  Stages now get their own `CLAUDE_CONFIG_DIR` (already allowlisted in
  `child_env` for exactly this), with `hooks: {}` and nothing else. It applies to
  every backend, not just anthropic — a DeepSeek stage runs the same CLI and
  would inherit the same hooks.

  Two things had to be seeded, and the first attempt got this wrong by assuming
  rather than testing: pointing `CLAUDE_CONFIG_DIR` at a fresh directory returns
  "Not logged in" even on macOS, where the token lives in the Keychain, because
  setting that variable switches the CLI to file-backed credential storage. The
  directory is seeded with a minimal `.claude.json` (identity keys only —
  `projects` and the caches are dropped, since carrying them would re-import
  what this change removes) and a `.credentials.json` copied from disk on
  Linux/WSL or exported from the login Keychain on macOS, re-seeded when the
  token is within 30 minutes of expiry.

  **Security trade-off, stated plainly:** on macOS this writes an OAuth token to
  disk (mode 0600, under `$HOME`, never in the repo) that would otherwise stay in
  the Keychain. `PIPELINE_ISOLATED_CONFIG=0` opts out, at the price of stages
  inheriting the operator's hooks again.

  Verified by hand before wiring: under the isolated config, four parallel
  subagents all completed and the hook's global counter never moved. Then in a
  live sandbox run, the developer stage reported
  `CONFIG_DIR: ~/.ai-delivery/claude-config` alongside the target's canary
  string, with all three lenses dispatched and a clean verdict.

- **Stages now run FROM the target project.** The previous entry's explicit
  read of `{target_repo}/CLAUDE.md` was a workaround for the real defect: the
  stage subprocess was spawned with no `cwd`, so it inherited the daemon's
  directory and Claude Code loaded ai-delivery's own instructions into every
  stage of every task. The reason given for not simply moving `cwd` — that
  personas resolve from `.claude/agents/` of the working directory and none are
  installed at user level — was wrong. `claude --agents <json>` injects persona
  definitions independently of the filesystem; verified before writing any code,
  in a directory containing only a `CLAUDE.md` and no `.claude/` at all, which
  both loaded that file as project instructions and dispatched an injected
  persona. Stages therefore run with `cwd` inside the task worktree (falling
  back to the target repo, then to this repo), with the 15 personas injected and
  `--add-dir` keeping the task artifacts and vendored templates reachable. Tool
  restrictions survive the trip — the read-only reviewer and the three lenses
  still carry `Read, Grep, Glob` only, which is the property that makes them
  physically unable to patch code. `model: inherit` is deliberately not
  forwarded: it is our convention for "same capability as the session", not a
  model id.

  Twelve prompt citations of `.claude/templates/...` were relative and would
  have resolved against the target tree — silently, since a stage that finds no
  file simply proceeds with the inlined copy of the pattern. They are absolute
  via a new `{pipeline_root}` placeholder, pinned by a test that rejects the
  relative form. The explicit read survives for `AGENTS.md` alone, which the
  harness does not load natively, and the prompts now say so — the reason has to
  travel with the instruction or a later edit drops it as redundant.

  Proved on the sandbox before landing: a full S-tier run (developer →
  tester+security → reviewer, all rc=0, $2.75, verdict `approve`) against a
  target whose `CLAUDE.md` mandates stdlib-only Python and `unittest`. The
  developer stage reported the target's canary string, and the code it wrote
  carries the target's conventions — type hints, a one-line docstring, guard
  clauses, `unittest`, no new dependency. All three review lenses dispatched
  through `--agents`.

- **Every stage was reading the wrong project's instructions.** The stage
  subprocess is spawned with no `cwd` override, so it inherits the daemon's
  working directory — this repo. Claude Code loads `CLAUDE.md` from the working
  directory at startup, so each stage of each task booted with *ai-delivery's*
  own `CLAUDE.md` (two-remote push policy, internal language policy) while
  working on a completely different repository, and never saw the target's.
  Nothing in `stage_prompts.py` referenced `CLAUDE.md` or `AGENTS.md` at all.
  The stages that reason about the target — discovery, ba, architect,
  pattern-detector, developer, developer-hotfix, tester — now read
  `{target_repo}/CLAUDE.md` and `{target_repo}/AGENTS.md` explicitly, with the
  reason travelling alongside the instruction so a later edit does not drop it
  as redundant. `research/topics/C_context.md` had already rated these files
  "a mandatory foundation"; the pipeline was the one place not using them.
  Moving `cwd` into the target worktree is the cleaner fix and is blocked on
  installing the personas at user level — the current `cwd` is what makes
  `.claude/agents/` resolvable.

- **The Developer persona no longer names a default language.** Upstream
  (VoltAgent) opened `backend-developer` with "deep expertise in Node.js 18+,
  Python 3.11+, and Go 1.21+", and that persona was dispatched for every
  developer and hotfix stage regardless of what the target repo was written in —
  a Node.js specialist writing Python here. It is now explicitly
  language-agnostic, with a stated order of authority for establishing the
  stack: the target's `CLAUDE.md` / `AGENTS.md` first, then the Pattern-Detection
  report and architecture proposal, then the repo's own manifests — and an
  instruction to state the ambiguity rather than pick silently when none of them
  settle it. Upstream's "Query context manager" step went with it: there is no
  context manager to query in this pipeline (`context-manager` is the Discovery
  stage's own persona). The removed wording is quoted in an HTML provenance
  comment so the next upstream sync notices the divergence, per the convention
  `.claude/agents/README.md` already documents. This removes the need for stack
  detection or per-language persona routing: the target states its own language.

- **Public mirror shipped a pipeline whose agents did not exist.** The publish
  filter excluded `.claude/` wholesale as "agent configuration and vendored
  templates", but `dispatcher/stage_prompts.py` dispatches subagents by the
  names defined in `.claude/agents/` — so a clone of the v1.0.0 mirror got a
  pipeline whose every stage referenced a persona that was not in the tree, and
  the new `tests/test_reviewer_lenses.py` (which reads `.claude/agents/`) could
  not pass there either. The directory is now split rather than dropped:
  `.claude/agents/**` and `.claude/commands/**` are published (27 files, scanned
  clean of host paths and private-repo references); `.claude/templates/**`
  (vendored BMAD + spec-kit, upstream licensing) and `.claude/settings.json`
  (operator harness config) stay private. The rule is fail-closed — a path under
  `.claude/` matching no keep-prefix is dropped, so a subdirectory added later
  stays private until it is explicitly listed.

- **A resumed task could no longer hotfix itself (branch/PR lock lost).** The
  Developer stage persists `state.branch` / `state.pr_url`; the resume path
  skips that stage when its artifact is already present, so a task that was
  limit-parked and auto-resumed reached the hotfix iteration with no lock — and
  the hotfix gate, correctly fail-closed, refuses a hotfix it cannot verify.
  A complete, fully green task died on it: the reviewer found one critical, the
  hotfix fixed it on the right branch and the right PR with 142/142 tests
  passing, and the runner still failed the task with `rc=5` after $14.56
  (`stt-local-path-source`, 2026-08-15). The lock is now re-derived from the
  Developer artifact when that stage is skipped on resume — from the artifact,
  never from git HEAD, since the lock records what the stage *did* and reading
  the current branch would defeat the drift check the gate exists to perform.
  Same class as #14 (cost/iteration/triage/base_branch lost across re-ingest);
  those four fields were covered, these two were not.

- **DeepSeek reachable again as a stage default (`DEEPSEEK_STAGES`).** The
  2026-06-07 two-model decision moved every stage onto anthropic, leaving
  DeepSeek reachable only through an explicit per-task `spec.model_routing` or
  the rate-limit cross-provider fallback — and the configured API key had since
  gone invalid, which the missing-key fallback does not catch (it fires on an
  *absent* key, not a rejected one). `DEEPSEEK_STAGES` is a comma list of stages
  whose *default* backend becomes DeepSeek, for the mechanical stages only;
  empty (the shipped default) changes nothing. The three existing safety nets
  still apply on top, in order: `spec.model_routing[stage]`, the tier-L
  anthropic force, and the iteration-2 escalation. Ignored with a warning when
  `DEEPSEEK_API_KEY` is unset; an unknown stage name warns instead of failing
  the dispatcher's boot. Five tests that still encoded the pre-2026-06-07
  all-DeepSeek default were fixed to drive the tier logic through an explicit
  cheap stage rather than assert a default that no longer exists.

- **Reviewer — three independent lenses + orchestrator-owned severity triage
  (#21).** The Reviewer stage was one `code-reviewer` subagent doing a single
  pass over "correctness vs BRD, tests, naming, coupling, ADR violations, scope
  creep" and self-assigning Critical/Warning/Suggestion in the same breath — and
  `.claude/agents/code-reviewer.md` was still unmodified VoltAgent boilerplate
  (checklist soup: "code coverage > 80% confirmed", "cyclomatic complexity < 10
  maintained") with no methodology and no evidence discipline. It is now three
  lenses dispatched in parallel with INDEPENDENT contexts — each gets the diff
  and its own brief, never the BRD and never another lens's findings:
  `blind-hunter` (context-free, forced quota of at least ten findings, so a
  "looks fine to me" pass is structurally impossible), `edge-case-hunter` (pure
  path tracer over the diff hunks, plus the deletion check: did removed code
  carry behavior or a contract the change neither re-established nor
  intentionally retired?), and `verification-gap` (the one genuinely new
  methodology — for each consumer, name the smallest realistic regression it
  would observe, then read the actual test and prove whether its assertion would
  fail; "read a test before claiming what it covers"; "before claiming no test
  exists, search the whole repo by symbol and import reference"). The stage
  orchestrator then does the triage itself as the **single severity authority**:
  dedupe by same-claim-and-same-action, confirm or refute each finding against
  the code it opens, dismiss anything it cannot ground, and disregard the
  severity a lens proposed — review subagents work under by-design information
  asymmetry and do not have enough context to set final severity for this
  workflow. Adapted from BMAD-METHOD v6.11.0 (MIT) per
  `research/bmad-steal-list.md` §2 items 1-3 / §4 rows 1-4; adapted, not
  vendored — every interactive halt is stripped (BMAD's review skills wait for a
  human menu choice, which hangs under `claude -p`), tools are restricted to
  `Read, Grep, Glob` in frontmatter, and three output dialects are unified into
  one findings block. Unchanged on purpose: the stage list and ordering, the
  `REVIEW_COMPLETE:` / `CRITICAL:` / `WARNING:` / `SUGGESTION:` verdict block the
  runner regex-parses, the hotfix loop contract, the `## Critical` heading the
  PR-comment builder slices from, the tier triage hint (nitpick guard), and the
  NOTIFY policy. New raw-lens audit trail per run: `review-lenses.md` in the task
  dir, holding what each lens said before triage (deliberately not an `NN-*.md`
  name — it is stage scratch, not a pipeline artifact). New tests:
  `tests/test_reviewer_lenses.py` — placeholder coverage across every stage
  prompt (a new `{placeholder}` without a matching kwarg used to surface only
  mid-run, after the upstream stages were already paid for), lens files present
  with the read-only tool header and no interactive halts, and the
  verdict-parsing contract asserted unchanged.
- **Chore — refreshed vendored BMAD/spec-kit templates + a repeatable
  drift-check mechanism (#21).** The one-shot 2026-05-26 vendor pull had
  frozen: BMAD-METHOD moved v6.8.0 → v6.11.0, spec-kit moved `c7e0cac` →
  `bf88c9f`, neither ever re-diffed against actual upstream source (the
  README's own refresh recipe used a stale `npx bmad-method install`
  sandbox path that no longer matches the current repo layout). Refreshed
  both `.claude/templates/bmad-v6/` and `.claude/templates/spec-kit/` to
  their current upstream content (pristine, no wiring into
  `SYSTEM_PROMPTS`/`STAGE_PROMPTS` — reference material only), added an
  `UPSTREAM` pin file to each recording source SHA/date/license and a
  per-file source mapping, and added `LICENSE` to `bmad-v6/` (previously
  attribution lived only in README prose). Two upstream restructurings
  found and handled explicitly rather than silently: `edge-case-hunter/`'s
  source skill is now a deprecated shim upstream (content re-sourced from
  the still-current embedded copy in `bmad-code-review`, local path kept
  stable since `dispatcher/stage_prompts.py` cites it); `document-project/`
  is fully deprecated upstream in favor of a much smaller
  `bmad-project-context` skill ("generating documentation volume... made
  agents worse, not better") — left frozen at its last-good content with a
  `DEPRECATED-UPSTREAM.md` note rather than blindly overwritten, since our
  discovery-stage prompt still depends on the old shape and picking a
  replacement is a product decision, not a vendoring one. `code-review/`
  picked up genuinely new upstream content: a `review_layers` array (Blind
  Hunter / Edge Case Hunter / Verification Gap Reviewer / Acceptance
  Auditor) and the Verification Gap methodology file flagged in
  `research/bmad-steal-list.md`. New mechanism:
  `ops/refresh-vendored-templates.sh` — shallow-clones both upstreams
  (pinnable via `BMAD_REF`/`SPECKIT_REF`), diffs the recorded file mapping,
  writes a unified-diff report to a printed temp path, and NEVER applies
  anything back into the repo; a pinned smoke run against the
  just-refreshed state reports `UP_TO_DATE`.
- **BA/Architect — BMAD steal-list "adapt" fragments (#21).** Three of the
  steal-list's `adapt`-verdict items, scoped to BA/Architect (reviewer-lens
  items — Blind Hunter, verification-gap, deletion-check, severity-authority
  separation — are a separate, parallel change): (1) the Architect's MADR ADR
  template gains a required **Prevents** field, distinct from Consequences —
  one line naming the specific divergence the decision rules out, not just
  its outcomes (`dispatcher/stage_prompts.py`, `.claude/agents/architect.md`).
  (2) The BA's self-ticked Quality Checklist gains a 4th section, **Theater
  Check** (NFR theater / persona-role theater / vision theater) — a
  mechanical, no-elicitation, no-halt self-review adapted from BMAD's PRD
  "substance over theater" rubric dimension, deliberately without its
  Glossary/UJ machinery (`dispatcher/stage_prompts.py`,
  `.claude/agents/business-analyst.md`). (3) New `dispatcher/architecture_lint.py`
  — a deterministic structural linter (ADR field completeness, C4 Mermaid
  presence, leftover placeholders) run right after the Architect stage,
  BEFORE the `analyze` stage's LLM pass, behind `ARCHITECTURE_LINT_ENABLED=1`
  (off by default); always report-only (`architecture-lint.md`), never blocks
  — a lighter, non-schema-adopting take on BMAD's `lint_spine.py`. New
  `tests/test_prompt_placeholders.py`: a generic placeholder-coverage guard
  over every `STAGE_PROMPTS` entry (`.format(**_build_format_kwargs(...))`
  must not `KeyError` for any stage), plus content-regression checks for the
  two prompt additions above.
- **Bugfix — silero-server `/synthesize` 500 on arm64, no traceback (#15).**
  `POST /synthesize` returned a bare 500 with nothing in container stdout, on
  atlas (arm64). Root cause (found via a real traceback, not the initial
  arch/torch-build suspicion — see below): two independent bugs, neither
  arch-specific, both live regardless of amd64 vs arm64. (1) `torch.hub.load`
  for a v3/v4/v5 Silero speaker (e.g. `MODEL_ID="v4_ru"`) returns a
  `(model, example_text)` tuple, not the model object — `server.py` bound
  the whole tuple to `_model`, so every call hit
  `AttributeError: 'tuple' object has no attribute 'apply_tts'`. (2) `numpy`
  was never listed in `requirements.txt`; `audio_tensor.cpu().numpy()`
  hard-requires it, so once (1) was fixed the handler still 500'd with
  `RuntimeError: Numpy is not available`. Fixed both: unpack the hub-load
  tuple correctly, and pin `numpy==1.26.4` in `silero-server/requirements.txt`.
  Also made errors visible going forward: the `/synthesize` handler's
  try/except now calls `logger.exception()` (full traceback to stdout) and
  returns a JSON body `{"error": <message>, "type": <exception class name>}`
  instead of a bare 500 / opaque `HTTPException` detail; `logging.basicConfig`
  moved out of the `if __name__ == "__main__"` guard (which never runs under
  the Dockerfile's `uvicorn server:app` CMD, so INFO/ERROR logs previously had
  no configured handler at all). Verified end-to-end on a throwaway
  arm64 build: `/synthesize` now returns a valid RIFF/WAVE PCM response.
  New test: `tests/test_silero_server_synthesize.py`.
- **Reliability — a killed runner no longer orphans its claude child (#18).**
  Operator kills of the stage runners left every spawned `claude` re-parented to
  init: one kept its agentic loop running for 3 h 11 m against the subscription
  with no task, no log and no owner (2026-08-14), and the same race let the
  abandoned child write `03-dev-agent.md` back into a re-created
  `active/<task>/` after the limit park had moved the task to `awaiting-input/`
  (split-brain dir with no state.json). New `dispatcher/proc_reaper.py` owns the
  child lifecycle: every stage child is spawned with `start_new_session=True`
  (leader of its own process group) and killed as a GROUP — TERM, then KILL
  after `ORPHAN_KILL_GRACE_SEC` (10 s) — so the CLI's subagent/MCP subtree, the
  part that actually burns tokens, dies with it. The group kill is wired into a
  SIGTERM/SIGINT handler on the runner itself (which then dies of the same
  signal, so its exit status stays truthful), into every stage exit path (limit
  stall, wall-clock timeout, buffered `LIMIT_STALL_DETECT=0` runs, any exception
  out of the stream reader), into leftover collection after a normal stage exit,
  and into `atexit` as a belt. Task-dir moves (limit park, clarify pause,
  handoff) now kill the runner's remaining children BEFORE the move, post-stage
  writes re-resolve the task dir across the buckets (`_task_dir_now`) instead of
  re-creating the path they started with, and `runner_state`'s breadcrumb writes
  drop + log a write into a vanished dir rather than raising. What SIGKILL
  leaves behind — no handler can catch it — is collected by a sweep over
  `ps -axo pid,ppid,tty,args`: the watcher runs it every
  `ORPHAN_SWEEP_INTERVAL_SEC` (60 s, one log line per kill) and
  `ops/atlas/aidstack.sh down` runs it once. The selection rule is deliberately
  narrow — `ppid == 1` **and** no controlling tty **and** both
  `--dangerously-skip-permissions` and `--output-format stream-json` on a
  process that is the claude CLI itself — because interactive Claude Code
  sessions on this machine carry the same `--dangerously` flag and must never be
  touched (near-miss 2026-08-12). `ORPHAN_SWEEP_ENABLED=0` disables the sweep;
  `tests/test_orphan_reaping.py` pins the whole safety table plus real-process
  proof that a SIGTERMed runner takes its child's group down.
- **Security — spawned `claude` children get a minimal env, not the operator's
  (#13).** Every child (stage runner, triage verdict call, bot sub-Claude,
  meta-agent) runs with `--dangerously-skip-permissions`; until now it inherited
  the FULL parent environment (`os.environ.copy()`, or no `env=` at all for the
  meta spawn), so any agent could read `TELEGRAM_BOT_TOKEN`, `OWNER_*`,
  `WINDMILL_TOKEN`, `CC_LANGSMITH_API_KEY`, `LITELLM_MASTER_KEY` with one `env`
  call in its Bash tool — a self-exfiltration surface log redaction cannot
  close. New `dispatcher/child_env.py:build_child_env()` constructs the child
  env by ALLOWLIST: base POSIX vars (`PATH`, `HOME`, `SHELL`, `USER`,
  `LOGNAME`, `TERM`, `TMPDIR`, `TZ`, `LANG`, `LC_*`), the harness/tooling vars
  the child itself reads (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`,
  `CLAUDE_CONFIG_DIR`, `SSH_AUTH_SOCK`, `XDG_*`, `NPM_CONFIG_PREFIX`, plus
  `JAVA_HOME`/`MAVEN_HOME`/`M2_HOME`/`GRADLE_HOME`/`SDKMAN_DIR` so target-repo
  builds still pick the right JDK), and the model/auth family of the ROUTED
  backend only — a DeepSeek stage no longer sees `GLM_API_KEY` and vice versa.
  Operators declare host-specific extras by name with
  `CHILD_ENV_EXTRA=NAME1,NAME2`. On the reference host a stage child goes from
  85 inherited variables to 17 (11 for an anthropic stage). Nothing that
  authenticates through files is affected: claude OAuth (`~/.claude`), `gh`
  (keyring / `XDG_CONFIG_HOME`), git identity and `bin/botctl-*` (which source
  `bot/.env` themselves) keep working. Covered by `tests/test_child_env.py`;
  see the "Secrets handling" subsection in `ARCHITECTURE.md` §8.
- **Reliability — bot-notification policy: no more spam on resume, stage list
  matches the actual plan (#19).** Owner feedback 2026-08-14: a re-ingest /
  limit-park resume fired FOUR Telegram messages for one event (`TRIAGE
  reused`, `agent-pipeline started`, `STAGE started`, `parked`), and several
  terminal-failure paths sent a plain "FAILED at stage" line immediately
  followed by the richer `_handoff_terminal` message for the same failure.
  New `dispatcher/notify_policy.py` — a pure `should_notify(event, verbose,
  is_resume)` decision table (`tests/test_notify_policy.py`) — is now the
  single gate every bot-notify call in `stage_runner_agent.py` goes through.
  Default policy: one message each for task start (rendered from the actual
  composed/triage-narrowed stage list — the same `stages` value the
  `[agent-pipeline] stages=...` log line prints, via the new
  `render_stage_list` helper, so the two can never drift apart), clarify
  questions, limit-park (unchanged — already deduped and task-id +
  one-line), auto-resume (new — the task-start slot switches to a "resumed —
  N/M stage(s) already done" message on any re-ingest, detected by the new
  `_task_is_resuming` helper), PR ready / awaiting-approval, and terminal
  failure. Per-stage start/finish/retry/recovered, triage diagnostics, and
  hotfix-loop progress are logged unconditionally (worklog.md + state
  history) and mirrored to the bot only under `NOTIFY_VERBOSE=1` (default
  off). Also de-duplicated: the PR-ready and "0 critical, ready to merge"
  messages merged into one send; the plain "PR parked for your review" /
  "stopped for your review" messages were dropped in favor of the
  `_park_budget_stop` button prompt that already fires immediately after for
  every reachable `awaiting-input` `stop_reason`.
- **Reliability — cost cap no longer bypassable via inbox requeue (#14).**
  Moving a mid-flight task dir back to `tasks/inbox/` — a manual requeue, or
  the limit-park auto-requeue sweep
  (`watcher.scan_limit_parked_for_resume`) — made
  `task_dispatcher._write_state_json` reset `cost_usd` (and `iteration`) to 0
  on re-ingest, even though the existing `state.json` carried the real
  accumulated spend. Since `run_pipeline` seeds its cost-cap check from
  `state.cost_usd`, the reset let the `cost_cap` be bypassed by repeated
  requeues: cumulative real spend could exceed the cap while every
  individual run's cap check still started from $0. `cost_usd`, `iteration`,
  `triage`, and `base_branch` now carry forward across re-ingest the same
  way `branch`/`pr_url`/`worktree` already did — a fresh spec (no prior
  `state.json`) still starts at 0. The watcher-respawn path
  (`stage_runner_agent.py`) was never affected — it reads `state.cost_usd`
  directly off disk. New `tests/test_cost_carry.py`.
- **Security — secret redaction on bot.py's logs.** `httpx`'s own INFO-level
  request logging embedded the Telegram bot token in plain text
  (`HTTP Request: POST https://api.telegram.org/bot<TOKEN>/getUpdates`) into
  `logs/bot.log` whenever `LOG_LEVEL` was DEBUG/INFO. Added `bot/log_redact.py`:
  a `logging.Filter` installed on bot.py's root logger AND its handlers
  (records propagated from child loggers like `httpx`/`telegram` only pass
  through handler-level filters, not the ancestor logger's own filter list —
  see the docstring on `install()`) that masks Telegram bot tokens
  (`bot\d+:[A-Za-z0-9_-]{30,}`) and the value of any env var whose name
  matches `.*(_KEY|_TOKEN|_SECRET|PASSWORD).*` (read once from `os.environ` at
  filter construction, longest-value-first so a short secret can't leave a
  truncated remainder of a longer one exposed). `httpx`/`httpcore` loggers are
  also capped at WARNING by default (`LOG_HTTP_LEVEL` env override). The
  meta-agent's raw stream-json event dump (`[<channel> event] {...}`, DEBUG)
  now passes through the same redaction and is truncated to
  `META_EVENT_LOG_MAX` (default 2000) chars per line. See the new "Secrets
  handling" subsection in `ARCHITECTURE.md` §8 for the full data-flow audit.
- **Reliability — limit outages are parked, not failed (#11).** A stage's
  `stream-json` output is now read INCREMENTALLY while the child runs
  (`dispatcher/limit_stall.py` + `stage_runner_agent._run_claude_stage`) instead
  of only after it exits. When the stream carries an api-error marker (429 /
  overloaded / session-limit), or goes silent for `LIMIT_STALL_WINDOW_SEC`
  (300 s) while `subagent_retry` events pile up, the runner kills the child
  early and returns the new `RC_LIMIT_STALL` (126) sentinel: the task is parked
  with `state.stage='waiting-limits'` + `resume_at` (the advertised reset time —
  `resets at <t>` / `retry-after` / `anthropic-ratelimit-*-reset` — else
  `LIMIT_BACKOFF_MIN`, 30 min) in `awaiting-input/`, the owner is pinged, and the
  watcher's sweep requeues it to `inbox/` once `resume_at` passes (the
  artifact-based resume then skips the completed stages). Previously the claude
  CLI's silent retry loop burned the full 30-minute stage timeout and the task
  was marked FAILED (2026-08-12: rc=124, $4.42, no artifact). Tunables:
  `LIMIT_STALL_WINDOW_SEC`, `LIMIT_STALL_MIN_RETRIES`, `LIMIT_BACKOFF_MIN`,
  `LIMIT_PARK_LIMIT`, `LIMIT_STALL_DETECT=0` (restore the buffered behaviour).
- **Safety — PR base branch enforced end-to-end (#10).** A task's PR could open
  against the repo's default branch instead of the registry-resolved base even
  though the branch itself was cut correctly (task `pin-frontend-node`
  opened the target repo's PR #4 against `dev-fix` instead of
  `feat/local-longpolling`, 2026-08-12) — the developer subagent's own
  `gh pr create` silently dropped
  `--base`. The rendered developer prompt now states `--base {base_branch}` as
  a non-negotiable RULE at both the orchestrator and nested-subagent level
  (previously only a numbered workflow step), and a new post-create backstop,
  `_verify_and_repair_pr_base` (`dispatcher/git_pr.py`), checks the opened PR's
  actual `baseRefName` against `state.base_branch` right after the existing
  `_branch_base_ok` gate and self-repairs a mismatch with one `gh pr edit
  --base` retry — logging loudly and recording a task-history warning if the
  repair itself fails. `_try_open_draft_pr` (the handoff-fallback PR path) was
  audited and already passed `--base` correctly; it was not the path that
  opened the PR in question.
- **Safety — Gated public-mirror publish script.** Added `scripts/publish-public.sh`: a fail-closed, idempotent operator script that exports a filtered git tree (stripping `STATE/`, `research/`, `briefs/`, `memory-bank/`, `.claude/`, `bot/projects.json`, `*.env*` non-examples, and most of `tasks/`), passes two independent secret/PII scans (gitleaks + a project-local grep blocklist), builds a single squash commit on the public mirror's `main`, and pushes only under an explicit `--push` flag. Added `ops/publish-blocklist.local.example`, `ops/PUBLISH-PUBLIC.md`, and a `--self-check` offline smoke mode. See `ops/PUBLISH-PUBLIC.md` for setup.
- **Safety — ephemeral worktree isolation (#6).** Each task's implementation
  stages (developer / tester / security / reviewer) now run in a throwaway
  `git worktree` of the target repo, created on the work branch cut fresh from
  `origin/<base>` and removed once the PR is pushed (the branch is kept). The
  pipeline no longer switches branches inside the caller's checkout — the first
  self-targeted run did exactly that and made the running deployment's own files
  vanish mid-run. It also unblocks parallel tasks against one target.
  `WORKTREE_ISOLATION_ENABLED=0` restores the legacy in-place behaviour;
  `WORKTREE_ROOT` moves the scratch directory.
- **Correctness — per-target base branch (#6).** `bot/projects.json` entries may
  now be `{"path": ..., "base": "dev"}`; the base a task branches from and PRs
  against resolves per-target → `PIPELINE_BASE_BRANCH` → the repo's own
  `origin/HEAD` → `main`, instead of a global `main` default that cut a run from
  a stale `master`. One shared parser (`dispatcher/project_registry.py`) reads
  the registry for both bot and dispatcher; the legacy plain-string entry form
  still works.
- **Triage — default flipped `off` → `shadow`.** `_triage_mode()`
  (`dispatcher/triage_wiring.py`) now defaults to `shadow` when `TRIAGE_MODE`
  is unset/empty/invalid, so every run at least classifies + logs the tier it
  would assign (still byte-identical pipeline — `shadow` never acts). The
  `off` mode remains available as an explicit opt-out, and `TRIAGE_DISABLED=1`
  is still the unconditional kill switch. Recommended deployment is
  `TRIAGE_MODE=s-only` (acts on S-tier only), now set in
  `ops/systemd/task-dispatcher.service`. (ai-delivery-private#2)
- **Reliability — cross-provider rate-limit fallback.** A stage that fails on a
  `429` / `five_hour` session limit now retries on a backend with **independent
  quota** (DeepSeek → GLM) instead of the futile same-provider retry — the failure
  mode that crashed three parallel anthropic tasks at once. An exhausted quota is
  reported honestly (`RC_RATE_LIMITED`, "re-queue after the window resets") instead
  of being mislabelled a crash. Opt out with `RATE_LIMIT_CROSS_PROVIDER_FALLBACK=0`.
- **Operator UX — self-contained notifications.** INVEST and graceful-handoff
  Telegram messages now inline a capped digest of the report *content* instead of a
  server-local filename a remote operator cannot open.
- **Refactor — god-module split.** `stage_runner_agent.py` decomposed from a
  4089-line monolith into nine single-responsibility modules behind a stable façade
  (−53%), with the full suite green at every step.
- **Docs** — README rewritten as a *control-plane / reliability layer over a frontier
  coding runtime* (honest scope: wraps Claude Code, single operator, no benchmark);
  added a demo GIF, a `gh auth` install step, and a runnable
  `dispatcher/examples/spec.example.json`.
- **Tests** — end-to-end `run_pipeline` integration test plus rate-limit-fallback and
  notification-digest regression tests (191 total).
- **Phase D** — removed the legacy subprocess runner (`dispatcher/stage_runner.py`,
  ~1.6k lines) and the `STAGE_RUNNER_MODE` switch. `stage_runner_agent.py` (the
  Agent-tool path, validated since committee Q3) is now the only runner; the
  watcher/dispatcher no longer branch on a mode. Docs updated to match.
- **Packaging** — declared the `jsonschema` dependency (the dispatcher needs it; a
  fresh `pip install -r bot/requirements.txt` previously left it crashing) and
  dropped the vendored `wmill` CLI from `windmill/wmill/` (~30% of repo size; it is
  an external npm tool — `windmill/README` documents installing it).
- **Clarify** — a `[NEEDS CLARIFICATION: <heading>] <question + default>` marker now
  surfaces both the heading and the trailing question to the operator (previously
  only the heading leaked through).

## v0.8 — Scheduler activation, adaptive-triage hardening, L-tier validated — 2026-06-03

- **Windmill scheduler activated** end-to-end (6-field cron fix) + `/schedule`.
- **Adaptive-triage hardening:** anti-thrash convergence gate (upgrade only on a
  strictly-decreasing reviewer critical trend), graceful handoff on
  timeout/non-convergence (park + `UNRESOLVED-FINDINGS.md`, never a silent fail),
  sticky triage across a clarify round-trip, BA re-run on clarify, nitpick guard
  (a `request_changes` with 0 criticals is mergeable, not a loop), per-tier reviewer
  hints.
- **Tier-aware execution:** L-tier runs the build/verify stages (developer / tester /
  security) on the strongest backend from iteration 0 and gets a longer per-stage
  wall-clock (`STAGE_TIMEOUT_SEC_L`).
- **Operator surface:** `/tasks` (queue listing per bucket) + `/requeue` (unblock a
  parked task), `/stt` (speech-to-text).
- **Recovery:** watcher orphan adoption + single-runner `flock`; stale-base backstop
  (feature branch cut fresh from `origin/<base>`).
- **Ops:** litellm + qdrant healthcheck / ulimit fixes; live task artifacts ignored.
- **History policy:** from v0.8 the public mirror is forwarded **granularly** (one
  sanitized commit per change); the squash merge was the one-time catch-up baseline.

## v0.7 — Adaptive complexity triage (opt-in) — 2026-05

- **Adaptive complexity triage** (`dispatcher/triage.py`, agent-harness
  path, default off). A feed-forward layer that sizes the pipeline to
  the task instead of running every task through the full route. One
  dial — `TRIAGE_MODE = off | shadow | s-only | full` (kill switch
  `TRIAGE_DISABLED=1`) — drives a staged rollout: classify-and-observe
  (`shadow`), then act on trivial tasks (`s-only`), then standard
  (`full`).
  - Classification: a deterministic pre-scan (file paths, counts, and a
    deterministic auth/crypto/migration/CI/payment path-risk check) plus
    an optional cheap-model verdict for the soft dimensions. Risk is
    deterministic and dominates (forces the full pipeline); otherwise
    size-L / underspecified / low-confidence → L, clear-tiny → S, else M.
  - Routing never drops the developer/test/security/review core — only
    the redundant upstream reasoning stages (discovery, pattern,
    architect, tasks, analyze, edge-cases). S-tier writes a deterministic
    lite BRD so downstream stages still have a spec.
  - **Budget in tokens, not dollars** (subscription-correct): per-tier
    `token_cap` + `iteration_cap`, enforced by a per-stage governor over
    `state.tokens_used`; env-tunable via `TRIAGE_{S,M,L}_TOKEN_CAP`. An
    upgrade ladder (S→M→L) recovers cheaply from underestimation.
  - Backward compatible: with `TRIAGE_MODE` off (the default) the
    pipeline is byte-identical to before. New `state.triage` /
    `state.tokens_used` fields and a `00a-triage.md` report are additive.
- INVEST validation report footer now reflects the gate's actual mode
  (BLOCK vs warn-only) instead of always claiming warn-only.

## v0.6 — Agent-tool harness (Compass arc) — 2026-05

- Pipeline stage runner gains an Agent-tool path (`STAGE_RUNNER_MODE=agent`)
  parallel to the subprocess path. Same artifacts, same Telegram side
  effects, same task-folder breadcrumbs — but each stage runs as a
  sub-agent inside the parent Claude session instead of a `subprocess.run`
  to the `claude` CLI.
- Pattern-Detection pre-Architect stage (opt-in, `01b-patterns.md`):
  catalogues conventions of the target repo so the Architect output
  follows existing naming, layering, error handling, and testing patterns
  instead of re-inventing them.
- Vendored upstream prompt templates verbatim in `.claude/templates/` —
  Spec-Kit (BA/Architect spec format) and BMAD v6 (analyst, architect,
  edge-case-hunter, code-review, document-project). Pipeline prompts now
  cite the vendored sources so changes upstream can be merged with a
  clear diff.

## v0.5 — Multi-backend routing via LiteLLM — 2026-04

- Per-stage backend selection: `model_routing` in `spec.json` lets BA and
  Architect run on Anthropic, while Dev/Test/Sec/Reviewer route through
  DeepSeek or GLM for cost reasons. Auto-escalation on the
  hotfix iteration loop: after 2 failed iterations on a non-Anthropic
  backend, the remaining stages flip back to Anthropic for the rest of
  the task.
- LiteLLM proxy support (`LITELLM_PROXY_URL`): opt-in routing through
  a local LiteLLM container instead of hitting providers directly.
  Required for unified observability across backends.

## v0.4 — Voice stack + semantic memory — 2026-03

- Whisper.cpp STT server: Telegram voice messages (.ogg) → transcript →
  the same `/task` pipeline as text.
- Silero TTS server: optional voice replies from the bot.
- `/memo` and `/recall` long-term memory backed by Qdrant + FastEmbed
  (`intfloat/multilingual-e5-large`, ONNX in-process — no Ollama). Hooks
  in `dispatcher/hooks/` auto-populate memory from end-of-turn assistant
  messages, subagent outputs, and pre-compaction history.

## v0.3 — Dispatcher + stage runner — 2026-02

- `dispatcher/` daemon turns `tasks/inbox/` into a folder-as-state-machine
  pipeline: `inbox → active → awaiting-approval → done | awaiting-input |
  failed`.
- 6-stage chain: BA → Architect → Developer → Tester → Security →
  Reviewer, each writing typed JSON sidecars (`NN-<stage>.json`) and
  Markdown artifacts (`NN-<stage>.md`) into the task folder.
- Hotfix iteration loop: if Reviewer requests changes, Developer-hotfix +
  Tester + Security + Reviewer rerun on the same branch / same PR until
  approve, iteration_cap, or cost_cap.
- Cost cap, watchdog, crash-recovery watcher (`dispatcher/watcher.py`).
- Windmill cron-triggered pipelines via
  `windmill/flows/pipeline-trigger.flow/`.

## v0.2 — meta-agent — 2026-01

- `bot/bot.py` routes Telegram messages to a `meta/CLAUDE.md`-instructed
  Claude Code session. Meta-agent decides whether to answer inline or
  delegate to a sub-Claude (`botctl-run-in-project`) in a target repo.
- `/run-in-project`, `/say`, `/status`, `/projects` Telegram commands.

## v0.1 — Telegram bot skeleton + botctl — 2025-12

- Minimal Telegram polling bot + `bin/botctl-*` helper scripts.
- `bot/.env`-driven secrets, per-host `bot/projects.json` registry.

## v1.0 — Initial public release

To be tagged when the public mirror is first pushed under
[two-remote layout](STATE/OPEN-SOURCE-USAGE.md). See `STATE/OPEN-SOURCE-MIGRATION.md`
for the migration story.
