# Keeping `master` current, and tagging a version

Two different things, often done in the same sitting. Do not confuse them —
conflating them is what let `master` sit 142 commits behind `dev`.

**Promoting `dev` to `master`** is routine and has no ceremony. `master` is the
repository's default branch: it is what a fresh clone gets, what a new harness
session is cut from, and what anything triggered against this repo runs. A
stale `master` triggers old code. So it gets `dev` as soon as work has landed
and CI is green.

**Tagging a version** is occasional. There is no build, no artifact, no deploy
— delivery is the pipeline's own Tester stage plus the operator's smoke gate.
A tag exists so a state of the tree can be named and returned to.

The branch model both assume is in [CONTRIBUTING.md](../CONTRIBUTING.md) →
"Branch model".

---

## Promote `dev` → `master`

Do this after a merge lands, and always before anything is triggered from
`master`.

- [ ] **CI is green on `dev`** — the run for the merge commit you are about to
      promote, not an older one. A red or missing run is a stop.

```bash
git fetch --prune origin
git checkout dev && git pull --ff-only origin dev

git checkout master && git pull --ff-only origin master
git merge --ff-only dev          # the healthy case
git push origin master

git checkout dev
```

If the fast-forward refuses, something landed on `master` out of band. Find out
what it was — but do not leave `master` behind over it. Once you know, bring
`dev` in with an ordinary merge and push:

```bash
git merge dev -m "merge: dev into master"
```

- [ ] CI green on `master` (the push triggers it).
- [ ] `git rev-list --count master..dev` is `0` — `master` carries everything
      `dev` does. This is the check that catches the failure this repo already
      had.

## Tag a version

- [ ] **CI is green on the commit being tagged.**
- [ ] **CHANGELOG has an `[Unreleased]` section that describes this release.**
      Rename it to the version and date as part of the release commit.
- [ ] **`STATE/CURRENT.md` matches reality** — someone reading it after the tag
      should recognise the state it describes.
- [ ] **Pick the version.** Pre-1.0 this project does not follow strict SemVer:
      bump the minor for a milestone worth naming, the patch for a fix release.

```bash
git checkout dev && git pull --ff-only origin dev
git tag -a vX.Y.Z -m "vX.Y.Z — <one line: what this milestone is>"
git push origin vX.Y.Z
```

Then promote `dev` to `master` per the section above, so the tag is reachable
from the default branch.

- [ ] `git merge-base --is-ancestor vX.Y.Z master` returns success — the tag is
      actually on the default branch, not only on `dev`. `v1.0.1` and `v1.0.2`
      were tagged on `dev` and never reached `master`; this is the assertion
      that would have caught it the first time.
- [ ] `STATE/CURRENT.md` notes the release.

## Publishing

Not part of either checklist. The public mirror is live but **no longer
refreshed** — publication has been paused since 2026-08-21 and resuming it is a
deliberate owner decision, run through `scripts/publish-public.sh` and nothing
else. See `CLAUDE.md` §1 and [PUBLISH-PUBLIC.md](PUBLISH-PUBLIC.md).
