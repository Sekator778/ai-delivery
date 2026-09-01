"""Task-scoped memory recall + typed write-back (dispatcher/memory_inject.py).

No test here touches the network: TEI/Qdrant transport is monkeypatched at
the module seams (_embed/_search/_post_json). The failure contract is the
load-bearing part — a stage must never fail or change behavior because the
memory infrastructure is down — so most tests assert the DEGRADED path.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import memory_inject as mi  # noqa: E402
from stage_prompts import STAGE_PROMPTS  # noqa: E402

_HIT = {"id": "p1", "score": 0.9,
        "payload": {"text": "prefer unittest discover", "source": "session_stop",
                    "timestamp": "2026-06-03T11:08:03+00:00"}}


def _clean_env():
    patcher = mock.patch.dict(os.environ, {}, clear=False)
    patcher.start()
    for var in ("MEMORY_INJECT_ENABLED", "MEMORY_WRITEBACK_ENABLED",
                "MEMORY_INJECT_STAGES", "MEMORY_TOP_K", "MEMORY_TARGET_CAP"):
        os.environ.pop(var, None)
    return patcher


class SlotContractTests(unittest.TestCase):
    """The runner's replace targets the literal SLOT — prompts must carry it."""

    def test_opted_in_stages_carry_the_slot(self) -> None:
        for stage in ("ba", "architect", "developer"):
            self.assertIn(mi.SLOT, STAGE_PROMPTS[stage], stage)

    def test_slot_survives_format(self) -> None:
        # .format on the prompt must not mangle the slot (it carries no
        # placeholders) — fill_slot runs on the FORMATTED prompt.
        self.assertNotIn("{", mi.SLOT)


class FillSlotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(_clean_env().stop)
        self.prompt = f"header\n{mi.SLOT}\nfooter"

    def test_replaces_slot_with_recalled_block(self) -> None:
        with mock.patch.object(mi, "recall", return_value=[_HIT]):
            out = mi.fill_slot(self.prompt, stage="ba", query="q",
                               target_repo="/repo")
        self.assertNotIn(mi.SLOT, out)
        self.assertIn("prefer unittest discover", out)
        self.assertIn("<injected-memory>", out)
        self.assertIn("</injected-memory>", out)
        self.assertTrue(out.startswith("header\n") and out.endswith("\nfooter"))

    def test_unchanged_when_disabled(self) -> None:
        os.environ["MEMORY_INJECT_ENABLED"] = "0"
        with mock.patch.object(mi, "recall", side_effect=AssertionError):
            self.assertEqual(
                mi.fill_slot(self.prompt, stage="ba", query="q",
                             target_repo="/r"), self.prompt)

    def test_unchanged_for_unlisted_stage(self) -> None:
        with mock.patch.object(mi, "recall", side_effect=AssertionError):
            self.assertEqual(
                mi.fill_slot(self.prompt, stage="reviewer", query="q",
                             target_repo="/r"), self.prompt)

    def test_unchanged_without_marker(self) -> None:
        with mock.patch.object(mi, "recall", side_effect=AssertionError):
            self.assertEqual(
                mi.fill_slot("no slot here", stage="ba", query="q",
                             target_repo="/r"), "no slot here")

    def test_unchanged_when_recall_empty(self) -> None:
        with mock.patch.object(mi, "recall", return_value=[]):
            self.assertEqual(
                mi.fill_slot(self.prompt, stage="ba", query="q",
                             target_repo="/r"), self.prompt)


class RecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(_clean_env().stop)

    def test_scoped_hits_come_first_and_dedupe(self) -> None:
        scoped = [{"id": "s1", "payload": {"text": "scoped"}}]
        global_ = [{"id": "s1", "payload": {"text": "scoped"}},
                   {"id": "g1", "payload": {"text": "global"}}]
        with mock.patch.object(mi, "_embed", return_value=[0.1] * 4), \
             mock.patch.object(mi, "_search",
                               side_effect=[scoped, global_]) as srch:
            hits = mi.recall("query", "/repo")
        self.assertEqual([h["id"] for h in hits], ["s1", "g1"])
        # First call scoped (target_repo passed), second global.
        self.assertEqual(srch.call_args_list[0].args[2], "/repo")

    def test_embed_failure_degrades_to_empty(self) -> None:
        with mock.patch.object(mi, "_embed", return_value=None):
            self.assertEqual(mi.recall("q", "/repo"), [])


class FormatBlockTests(unittest.TestCase):
    def test_entries_are_numbered_trimmed_and_capped(self) -> None:
        long_hit = {"id": "x", "payload": {"text": "word " * 500,
                                           "kind": "task_lesson",
                                           "timestamp": "2026-08-15T00:00:00"}}
        block = mi.format_block([_HIT, long_hit, {"id": "e", "payload": {}}])
        self.assertIn("1. (2026-06-03 session_stop)", block)
        self.assertIn("2. (2026-08-15 task_lesson)", block)
        self.assertNotIn("3.", block)  # empty-text hit skipped
        self.assertLessEqual(len(block), mi._BLOCK_CHAR_CAP)


class WriteBackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(_clean_env().stop)
        self.state = {"tier": "S", "iteration": 1, "cost_usd": 3.33,
                      "pr_url": "https://x/pr/1"}

    def test_upserts_typed_point_and_retires(self) -> None:
        calls = []

        def fake_post(url, payload, method="POST"):
            calls.append((url, payload))
            return {"status": "ok", "result": {"points": []}}

        with mock.patch.object(mi, "_embed", return_value=[0.1] * 4), \
             mock.patch.object(mi, "_post_json", side_effect=fake_post):
            ok = mi.write_back(task_id="t1", target_repo="/repo",
                               spec_prompt="add median()", state=self.state,
                               stop_reason="approve")
        self.assertTrue(ok)
        upsert_url, upsert = calls[0]
        self.assertIn("/points?wait=true", upsert_url)
        payload = upsert["points"][0]["payload"]
        self.assertEqual(payload["kind"], "task_lesson")
        self.assertEqual(payload["target_repo"], "/repo")
        self.assertEqual(payload["tier"], "S")
        self.assertIn("add median()", payload["text"])
        self.assertIn("$3.33", payload["text"])

    def test_disabled_env_skips(self) -> None:
        os.environ["MEMORY_WRITEBACK_ENABLED"] = "0"
        with mock.patch.object(mi, "_embed", side_effect=AssertionError):
            self.assertFalse(mi.write_back(
                task_id="t", target_repo="/r", spec_prompt="p",
                state={}, stop_reason="approve"))

    def test_embed_failure_returns_false(self) -> None:
        with mock.patch.object(mi, "_embed", return_value=None):
            self.assertFalse(mi.write_back(
                task_id="t", target_repo="/r", spec_prompt="p",
                state={}, stop_reason="approve"))


class RetireCapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(_clean_env().stop)
        os.environ["MEMORY_TARGET_CAP"] = "2"

    def _points(self, n):
        return [{"id": f"p{i}", "payload": {"timestamp": f"2026-08-{i + 1:02d}"}}
                for i in range(n)]

    def test_deletes_oldest_beyond_cap(self) -> None:
        calls = []

        def fake_post(url, payload, method="POST"):
            calls.append((url, payload))
            if url.endswith("/scroll"):
                return {"result": {"points": self._points(4)}}
            return {"status": "ok"}

        with mock.patch.object(mi, "_post_json", side_effect=fake_post):
            mi._retire_over_cap("/repo")
        delete_calls = [c for c in calls if "/delete" in c[0]]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(delete_calls[0][1]["points"], ["p0", "p1"])

    def test_under_cap_deletes_nothing(self) -> None:
        calls = []

        def fake_post(url, payload, method="POST"):
            calls.append(url)
            return {"result": {"points": self._points(2)}}

        with mock.patch.object(mi, "_post_json", side_effect=fake_post):
            mi._retire_over_cap("/repo")
        self.assertFalse([u for u in calls if "/delete" in u])


class EphemeralTargetPortabilityTests(unittest.TestCase):
    """The predicate must answer the same on every host (backlog/T20).

    `gettempdir()` answers "is this ephemeral *here*". The store is portable —
    the JSONL travels in git and gets inspected from Linux, while the points
    were written on macOS, whose $TMPDIR is /var/folders/<xx>/<yyy>/T/. Before
    this was fixed the same record was ephemeral on one machine and legitimate
    on another, so a purge run from the wrong host silently found nothing.
    """

    MACOS_TMP = [
        "/var/folders/nt/0tkgtyt96_v12yx82b79b5bw0000gn/T/tmpwcn_cr87/repo",
        "/private/var/folders/ab/cdefgh/T/tmpx/repo",
    ]
    NOT_TMP = [
        "/Users/someone/projects/ai-delivery",
        "/var/folders-not-a-tmpdir/x",
        "/var/folders/only/two/parts",
        "/home/someone/repo",
        "",
    ]

    def test_macos_tmpdir_recognised_from_any_host(self) -> None:
        for path in self.MACOS_TMP:
            self.assertTrue(mi._is_ephemeral_target(path), path)

    def test_real_targets_are_not_ephemeral(self) -> None:
        for path in self.NOT_TMP:
            self.assertFalse(mi._is_ephemeral_target(path), path)

    def test_local_tempdir_still_recognised(self) -> None:
        import tempfile as _tf
        self.assertTrue(
            mi._is_ephemeral_target(os.path.join(_tf.gettempdir(), "tmpx", "repo")))


class SuiteIsolationTests(unittest.TestCase):
    """The suite must not be able to write into a live memory store.

    backlog/T02: `stage_runner_agent` calls `write_back` unconditionally at
    pipeline completion, `MEMORY_WRITEBACK_ENABLED` defaults to 1 and
    `MEMORY_QDRANT_URL` to localhost, so before 2026-08-20 any runner-level
    test that reached completion appended a real point to whatever Qdrant was
    listening. 21 of the 22 `task_lesson` points in the operator's live
    collection were that: targets under `$TMPDIR`.

    The load-bearing guard is `_is_ephemeral_target` in the module, not the
    environment: `python -m unittest discover -s tests` — the command this
    project documents — never imports `tests/__init__.py`, because
    `top_level_dir` defaults to the start directory and every test module is
    loaded as a top-level module. `test_discover_does_not_import_the_package`
    pins that fact, so a future reader does not re-derive the env guard as
    sufficient.

    Every probe runs in a subprocess with the MEMORY_* variables stripped, so
    it measures the code and not what this process inherited.
    """

    _PROBE = """
import sys, urllib.request
REPO_ROOT = {root!r}
sys.path.insert(0, REPO_ROOT)
{import_tests}
sys.path.insert(0, REPO_ROOT + "/dispatcher")
import memory_inject as mi

calls = []
def _recording_urlopen(req, *a, **k):
    calls.append(getattr(req, "full_url", str(req)))
    raise OSError("network disabled in this probe")
urllib.request.urlopen = _recording_urlopen

result = mi.write_back(
    task_id="TASK-PROBE", target_repo={target!r},
    spec_prompt="probe", state={{}}, stop_reason="done")
print("RESULT", result)
print("CALLS", len(calls))
"""

    def _run_probe(self, *, import_tests: bool, target: str) -> str:
        env = os.environ.copy()
        for var in ("MEMORY_WRITEBACK_ENABLED", "MEMORY_INJECT_ENABLED",
                    "MEMORY_QDRANT_URL", "MEMORY_TEI_URL"):
            env.pop(var, None)
        code = self._PROBE.format(
            root=str(REPO_ROOT), target=target,
            import_tests="import tests  # installs the env guard" if import_tests
            else "# deliberately NOT importing the tests package",
        )
        proc = subprocess.run([sys.executable, "-c", code], env=env,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0,
                         f"probe crashed:\n{proc.stdout}\n{proc.stderr}")
        return proc.stdout

    def test_ephemeral_target_never_reaches_the_network(self) -> None:
        """The guard that holds under `discover -s tests`: no env, no package."""
        target = os.path.join(tempfile.gettempdir(), "tmpprobe1234", "repo")
        out = self._run_probe(import_tests=False, target=target)
        self.assertIn("RESULT False", out,
                      "write_back accepted a target under the system temp dir")
        self.assertIn("CALLS 0", out,
                      "write_back reached the network for an ephemeral target")

    def test_a_real_target_would_reach_the_network(self) -> None:
        """Negative control — without it the test above proves nothing.

        A non-temp target with no env guard must attempt the write, or the
        refusal above is not what is protecting the live store.
        """
        out = self._run_probe(import_tests=False, target=str(REPO_ROOT))
        self.assertNotIn("CALLS 0", out,
                         "write_back made no network call for a real target, "
                         "so neither guard is what is protecting the store")

    def test_importing_the_tests_package_blocks_write_back(self) -> None:
        """The env guard, for the invocations that do import the package."""
        out = self._run_probe(import_tests=True, target=str(REPO_ROOT))
        self.assertIn("RESULT False", out)
        self.assertIn("CALLS 0", out)

    def test_guard_honours_an_explicit_operator_override(self) -> None:
        # setdefault, not assignment: an operator who deliberately runs the
        # suite against a live store keeps their override.
        env = os.environ.copy()
        env["MEMORY_WRITEBACK_ENABLED"] = "1"
        proc = subprocess.run(
            [sys.executable, "-c",
             f"import sys, os; sys.path.insert(0, {str(REPO_ROOT)!r}); "
             "import tests; print(os.environ['MEMORY_WRITEBACK_ENABLED'])"],
            env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "1")

    def test_discover_does_not_import_the_package(self) -> None:
        """Pin the reason the module-level guard has to exist.

        If a future CPython (or a changed suite command) starts importing
        `tests/__init__.py` under `discover -s tests`, this fails and the
        comments above can be simplified. Until then, do not move the guard
        back into the environment.
        """
        probe = REPO_ROOT / "tests" / "test_zz_discovery_probe.py"
        probe.write_text(
            "import os, unittest\n"
            "print('PKG_IMPORTED=', os.environ.get('MEMORY_WRITEBACK_ENABLED'))\n"
            "class T(unittest.TestCase):\n"
            "    def test_noop(self):\n"
            "        pass\n"
        )
        self.addCleanup(probe.unlink, missing_ok=True)
        env = os.environ.copy()
        env.pop("MEMORY_WRITEBACK_ENABLED", None)
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests",
             "-p", "test_zz_discovery_probe.py"],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True)
        self.assertIn("PKG_IMPORTED= None", proc.stdout + proc.stderr,
                      "discover -s tests now imports tests/__init__.py — the "
                      "env guard would cover the suite; revisit the comments "
                      "in tests/__init__.py and memory_inject._is_ephemeral_target")


if __name__ == "__main__":
    unittest.main()
