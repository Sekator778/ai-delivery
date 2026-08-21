"""Test package for ai-delivery.

Belt-and-braces guard against a suite run touching a live memory store
(backlog/T02, 2026-08-20).

`dispatcher/stage_runner_agent.py` calls `memory_inject.write_back(...)`
unconditionally when a pipeline run completes. `MEMORY_WRITEBACK_ENABLED`
defaults to `1` and `MEMORY_QDRANT_URL` to `http://127.0.0.1:6333`, so a
runner-level test that reached completion appended a real point to whatever
Qdrant was listening on the developer's machine — silently, because the
module degrades to a no-op rather than complaining. 21 of the 22 `task_lesson`
points in the operator's live collection were exactly that.

**This file is not the load-bearing guard.** With the command this project
documents — `python -m unittest discover -s tests` — `top_level_dir` defaults
to the start directory, so every test module is imported as a *top-level*
module (`test_triage`, not `tests.test_triage`) and this package is never
imported at all. Verified 2026-08-20 on CPython 3.11; the import runs only
under `discover -s tests -t .`, `python -m unittest tests.<module>`, or pytest.

The guard that always holds lives in `dispatcher/memory_inject.write_back`,
which refuses any `target_repo` under the system temp directory — see
`_is_ephemeral_target` there for why that is the right rule on its own terms,
independent of tests. What follows only widens the net for the invocations
that do import this package.

`setdefault`, not assignment — an operator deliberately running the suite
against a live store keeps their override. tests/test_memory_inject.py pins
both halves of this contract.
"""

import os

os.environ.setdefault("MEMORY_WRITEBACK_ENABLED", "0")
os.environ.setdefault("MEMORY_INJECT_ENABLED", "0")
