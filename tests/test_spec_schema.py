"""Tests for dispatcher/schema/spec.schema.json — trigger=manual support.

Run with: bot/venv/bin/python -m unittest tests/test_spec_schema.py

Requires the project venv (bot/venv/bin/python, Python 3.12, jsonschema 4.23.0).
The ambient python3 on this host (3.9) lacks jsonschema; running under a plain
interpreter raises ImportError immediately, which is intentional (ADR-003):
a skipped regression guard is indistinguishable from a passing one.

Global names in task_dispatcher that IngestManualSpecTests patches:
  td.INBOX_DIR, td.ACTIVE_DIR, td.FAILED_DIR, td.TASKS_DIR,
  td._spawn_stage_runner
If any of these names change in task_dispatcher.py, update setUp/tearDown.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema  # module-scope import, intentional — see ADR-003

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import task_dispatcher as td  # noqa: E402

SCHEMA_PATH = REPO_ROOT / "dispatcher" / "schema" / "spec.schema.json"
EXAMPLES_DIR = REPO_ROOT / "dispatcher" / "examples"


def _load_schema() -> dict:
    with SCHEMA_PATH.open() as f:
        return json.load(f)


def _validate(instance: dict, schema: dict) -> None:
    """Raise jsonschema.ValidationError if instance is invalid."""
    jsonschema.validate(instance=instance, schema=schema)


# ---------------------------------------------------------------------------
# Base instance — each table row starts here, then adds/overrides fields.
# ---------------------------------------------------------------------------

_BASE: dict = {
    "user": "op",
    "prompt": "p",
    "target_repo": "/tmp/x",
}


class SpecSchemaTriggerTests(unittest.TestCase):
    """Table-driven schema contract tests — 12 rows per architecture §Test strategy.

    Rows 1-3 pin FR-001/FR-002/FR-011 (manual trigger).
    Row  4   pins FR-008 (telegram regression guard).
    Row  5   pins FR-003 (windmill backward-compat).
    Rows 6-7 pin FR-009 + Edge case 5 (bogus / case-variant triggers).
    Row  8   pins FR-012 (no free-text source field).
    Row  9   pins Edge case 6 (partial telegram_thread).
    Row 10   pins Edge case 1 (telegram + explicit null thread loophole).
    Row 11   pins Edge case 2 (missing trigger gives misleading error).
    Row 12   pins the telegram positive-control path (telegram + full valid
             thread is accepted) — added while porting PR #7 onto the dev
             base (dev base validation-path check, #1).
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = _load_schema()

    def _assert_valid(self, instance: dict, msg: str = "") -> None:
        try:
            _validate(instance, self.schema)
        except jsonschema.ValidationError as e:
            self.fail(
                f"Expected valid but got: {e.message!r}"
                + (f" — {msg}" if msg else "")
            )

    def _assert_invalid(
        self, instance: dict, *, expect_in_message: str = "", msg: str = ""
    ) -> str:
        """Assert instance is invalid and return the error message string."""
        try:
            _validate(instance, self.schema)
        except jsonschema.ValidationError as e:
            if expect_in_message:
                self.assertIn(
                    expect_in_message,
                    e.message,
                    f"Error {e.message!r} lacks {expect_in_message!r}"
                    + (f" — {msg}" if msg else ""),
                )
            return e.message
        self.fail(
            "Expected invalid but validation succeeded"
            + (f" — {msg}" if msg else "")
        )
        return ""  # unreachable; satisfies type checker

    # ------------------------------------------------------------------ #
    # Row 1 — trigger=manual, no telegram_thread                          #
    # ------------------------------------------------------------------ #
    def test_01_manual_no_thread_is_valid(self) -> None:
        """FR-001, FR-002: trigger=manual without telegram_thread must pass."""
        inst = {**_BASE, "trigger": "manual"}
        self._assert_valid(inst, "FR-001/002 manual trigger without thread")

    # ------------------------------------------------------------------ #
    # Row 2 — trigger=manual, telegram_thread=null                        #
    # ------------------------------------------------------------------ #
    def test_02_manual_thread_null_is_valid(self) -> None:
        """FR-002: explicit null telegram_thread is accepted for manual."""
        inst = {**_BASE, "trigger": "manual", "telegram_thread": None}
        self._assert_valid(inst, "FR-002 explicit null thread")

    # ------------------------------------------------------------------ #
    # Row 3 — trigger=manual, telegram_thread with chat_id + message_id  #
    # ------------------------------------------------------------------ #
    def test_03_manual_with_full_thread_is_valid(self) -> None:
        """FR-011: manual trigger may supply telegram_thread for status routing."""
        inst = {
            **_BASE,
            "trigger": "manual",
            "telegram_thread": {"chat_id": 123456, "message_id": 9876},
        }
        self._assert_valid(inst, "FR-011 manual with full thread")

    # ------------------------------------------------------------------ #
    # Row 4 — trigger=telegram, no telegram_thread (FR-008 regression)   #
    # ------------------------------------------------------------------ #
    def test_04_telegram_without_thread_is_invalid(self) -> None:
        """FR-008 regression guard: telegram without thread must still fail."""
        inst = {**_BASE, "trigger": "telegram"}
        self._assert_invalid(
            inst,
            expect_in_message="telegram_thread",
            msg="FR-008 regression guard",
        )

    # ------------------------------------------------------------------ #
    # Row 5 — trigger=windmill, no telegram_thread                        #
    # ------------------------------------------------------------------ #
    def test_05_windmill_without_thread_is_valid(self) -> None:
        """FR-003: windmill without thread must remain valid (backward compat)."""
        inst = {**_BASE, "trigger": "windmill"}
        self._assert_valid(inst, "FR-003 windmill backward-compat")

    # ------------------------------------------------------------------ #
    # Row 6 — trigger=bogus                                               #
    # ------------------------------------------------------------------ #
    def test_06_bogus_trigger_is_invalid(self) -> None:
        """FR-009: an unrecognized trigger value must fail with an enum error."""
        inst = {**_BASE, "trigger": "bogus"}
        msg = self._assert_invalid(inst, msg="FR-009 bogus trigger")
        self.assertIn("'bogus' is not one of", msg)

    # ------------------------------------------------------------------ #
    # Row 7 — trigger=MANUAL (wrong case)                                 #
    # ------------------------------------------------------------------ #
    def test_07_trigger_case_variant_is_invalid(self) -> None:
        """Edge case 5: enum is case-sensitive; MANUAL, Manual, etc. are rejected."""
        inst = {**_BASE, "trigger": "MANUAL"}
        self._assert_invalid(inst, msg="Edge case 5 — case sensitivity")

    # ------------------------------------------------------------------ #
    # Row 8 — trigger=manual + undeclared source field                    #
    # ------------------------------------------------------------------ #
    def test_08_manual_with_extra_source_field_is_invalid(self) -> None:
        """FR-012: additionalProperties:false must reject undeclared 'source'."""
        inst = {**_BASE, "trigger": "manual", "source": "monitoring"}
        self._assert_invalid(
            inst,
            expect_in_message="Additional properties are not allowed",
            msg="FR-012 no free-text source field",
        )

    # ------------------------------------------------------------------ #
    # Row 9 — trigger=manual, partial telegram_thread (missing message_id)#
    # ------------------------------------------------------------------ #
    def test_09_manual_partial_thread_is_invalid(self) -> None:
        """Edge case 6: telegram_thread with only chat_id (no message_id) must fail."""
        inst = {**_BASE, "trigger": "manual", "telegram_thread": {"chat_id": 123}}
        self._assert_invalid(
            inst,
            expect_in_message="message_id",
            msg="Edge case 6 — partial thread",
        )

    # ------------------------------------------------------------------ #
    # Row 10 — trigger=telegram, explicit null telegram_thread            #
    # ------------------------------------------------------------------ #
    def test_10_telegram_explicit_null_thread_is_valid(self) -> None:
        """Edge case 1 (documented loophole): explicit null satisfies 'required'.

        'required' checks key *presence*; telegram_thread is typed ["object","null"]
        so null satisfies presence.  Pinned per ADR-002 as a deliberate known
        quirk — fixing it would alter telegram behavior (FR-003/NFR-002).
        """
        inst = {**_BASE, "trigger": "telegram", "telegram_thread": None}
        self._assert_valid(inst, "Edge case 1 — explicit null satisfies required")

    # ------------------------------------------------------------------ #
    # Row 11 — no trigger key at all                                      #
    # ------------------------------------------------------------------ #
    def test_11_missing_trigger_reports_telegram_thread_error(self) -> None:
        """Edge case 2: the allOf if/then fires vacuously when trigger is absent.

        The if clause {"properties": {"trigger": {"const": "telegram"}}} matches
        an object that lacks trigger entirely (absent key is vacuously valid under
        'properties'), so 'then' fires and jsonschema surfaces 'telegram_thread'
        is required rather than 'trigger' is required.  Pinned per ADR-002 as a
        documented quirk; the fix requires the oneOf restructure rejected there.
        """
        inst = {**_BASE}  # trigger key intentionally absent
        msg = self._assert_invalid(
            inst, msg="Edge case 2 — misleading telegram_thread error"
        )
        self.assertIn(
            "telegram_thread",
            msg,
            f"Expected 'telegram_thread' in error, got: {msg!r}",
        )

    # ------------------------------------------------------------------ #
    # Row 12 — trigger=telegram, full telegram_thread (positive control) #
    # ------------------------------------------------------------------ #
    def test_12_telegram_with_full_thread_is_valid(self) -> None:
        """Positive control complementing Row 4: a genuinely well-formed
        telegram submission (chat_id + message_id present) must still
        validate. Guards against a manual-trigger change accidentally
        tightening the telegram branch of the allOf/if/then.
        """
        inst = {
            **_BASE,
            "trigger": "telegram",
            "telegram_thread": {"chat_id": 123456, "message_id": 9876},
        }
        self._assert_valid(inst, "telegram with full thread — positive control")


class SpecExamplesValidateTests(unittest.TestCase):
    """Validate every *.json fixture under dispatcher/examples/ against the schema.

    Acts as a permanent drift guard (ADR-004): any committed example file must
    be schema-valid.  Covers spec.example.json (trigger=windmill, verified today)
    and spec.manual.example.json (trigger=manual, added for FR-010).
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = _load_schema()

    def test_all_example_fixtures_are_schema_valid(self) -> None:
        examples = sorted(EXAMPLES_DIR.glob("*.json"))
        self.assertGreater(
            len(examples),
            0,
            f"No *.json fixtures found in {EXAMPLES_DIR}",
        )
        for path in examples:
            with self.subTest(fixture=path.name):
                instance = json.loads(path.read_text())
                try:
                    _validate(instance, self.schema)
                except jsonschema.ValidationError as e:
                    self.fail(f"{path.name} fails schema validation: {e.message}")


class IngestManualSpecTests(unittest.TestCase):
    """Integration tests for _ingest() with trigger=manual.

    Monkey-patches task_dispatcher's directory globals to a temp tree so the
    test never touches the live tasks/ queue and never spawns a real pipeline
    (ADR-005).

    Patched globals: td.INBOX_DIR, td.ACTIVE_DIR, td.FAILED_DIR, td.TASKS_DIR.
    td._spawn_stage_runner is replaced by a recording stub.
    """

    def setUp(self) -> None:
        self._schema = _load_schema()
        self._tmp = Path(tempfile.mkdtemp())

        # Minimal tasks/ tree inside the temp dir
        self._inbox = self._tmp / "inbox"
        self._active = self._tmp / "active"
        self._failed = self._tmp / "failed"
        for d in (self._inbox, self._active, self._failed):
            d.mkdir()

        # Save originals
        self._orig = {
            "INBOX_DIR": td.INBOX_DIR,
            "ACTIVE_DIR": td.ACTIVE_DIR,
            "FAILED_DIR": td.FAILED_DIR,
            "TASKS_DIR": td.TASKS_DIR,
            "_spawn_stage_runner": td._spawn_stage_runner,
        }

        # Patch module globals
        td.INBOX_DIR = self._inbox
        td.ACTIVE_DIR = self._active
        td.FAILED_DIR = self._failed
        td.TASKS_DIR = self._tmp

        # Recording stub for _spawn_stage_runner
        self._spawned: list[Path] = []
        td._spawn_stage_runner = lambda task_dir: self._spawned.append(task_dir)

    def tearDown(self) -> None:
        td.INBOX_DIR = self._orig["INBOX_DIR"]
        td.ACTIVE_DIR = self._orig["ACTIVE_DIR"]
        td.FAILED_DIR = self._orig["FAILED_DIR"]
        td.TASKS_DIR = self._orig["TASKS_DIR"]
        td._spawn_stage_runner = self._orig["_spawn_stage_runner"]
        shutil.rmtree(str(self._tmp), ignore_errors=True)

    def _make_spec_dir(self, folder_name: str, spec: dict) -> Path:
        """Create inbox/<folder_name>/spec.json and return the path to spec.json."""
        folder = self._inbox / folder_name
        folder.mkdir()
        spec_path = folder / "spec.json"
        spec_path.write_text(json.dumps(spec))
        return spec_path

    # ------------------------------------------------------------------ #
    # FR-004 happy path: manual spec is ingested to active/               #
    # ------------------------------------------------------------------ #
    def test_fr004_manual_happy_path(self) -> None:
        """FR-004: manual spec produces the same file artifacts as windmill."""
        spec = {
            "trigger": "manual",
            "user": "op",
            "prompt": "test task for manual ingest",
            "target_repo": "/tmp/test",
            "task_id": "manual-ingest-test",
        }
        spec_path = self._make_spec_dir("manual-ingest-test", spec)

        td._ingest(spec_path, self._schema)

        task_dir = self._active / "manual-ingest-test"
        self.assertTrue(task_dir.exists(), "task directory not created in active/")

        # Required artifacts must all be written
        for artifact in ("task.md", "state.json", "worklog.md"):
            self.assertTrue(
                (task_dir / artifact).exists(),
                f"{artifact} not written for manual task",
            )

        # task.md must record the trigger as "manual"
        task_md = (task_dir / "task.md").read_text()
        self.assertIn("- **Source:** manual", task_md)

        # state.json must carry source=manual and telegram_thread=None
        state = json.loads((task_dir / "state.json").read_text())
        self.assertEqual(state["source"], "manual")
        self.assertIsNone(state["telegram_thread"])

        # The _spawn_stage_runner stub must have been called exactly once
        self.assertEqual(len(self._spawned), 1)
        self.assertEqual(self._spawned[0], task_dir)

    # ------------------------------------------------------------------ #
    # FR-004 parity: manual and windmill produce identically shaped state #
    # ------------------------------------------------------------------ #
    def test_fr004_manual_parity_with_windmill(self) -> None:
        """FR-004 parity: state.json shape is identical for manual and windmill.

        This is the machine-checked half of the 'identical ingestion sequence'
        claim (ADR-006 relies on it).  Fields that legitimately differ are
        excluded from the comparison.
        """
        base_spec = {
            "user": "op",
            "prompt": "parity test",
            "target_repo": "/tmp/test",
        }

        # Ingest manual
        spec_m = {**base_spec, "trigger": "manual", "task_id": "parity-manual"}
        td._ingest(self._make_spec_dir("parity-manual", spec_m), self._schema)

        # Ingest windmill (same temp tree)
        spec_w = {**base_spec, "trigger": "windmill", "task_id": "parity-windmill"}
        td._ingest(self._make_spec_dir("parity-windmill", spec_w), self._schema)

        state_m = json.loads((self._active / "parity-manual" / "state.json").read_text())
        state_w = json.loads((self._active / "parity-windmill" / "state.json").read_text())

        # Fields that MUST differ by design
        ignore = {"id", "title", "source", "submitted_at", "history"}
        for key in sorted(set(state_m) | set(state_w)):
            if key in ignore:
                continue
            with self.subTest(key=key):
                self.assertEqual(
                    state_m.get(key),
                    state_w.get(key),
                    f"state.json[{key!r}] differs between manual and windmill",
                )

    # ------------------------------------------------------------------ #
    # FR-009: bogus trigger is quarantined, not ingested                  #
    # ------------------------------------------------------------------ #
    def test_fr009_bogus_trigger_quarantines(self) -> None:
        """FR-009: spec with unrecognized trigger lands in failed/ with reason.txt."""
        spec = {
            "trigger": "bogus",
            "user": "op",
            "prompt": "bad trigger",
            "target_repo": "/tmp/test",
            "task_id": "bogus-test",
        }
        spec_path = self._make_spec_dir("bogus-test", spec)

        td._ingest(spec_path, self._schema)

        # active/ must stay empty
        self.assertEqual(
            list(self._active.iterdir()),
            [],
            "active/ should be empty after quarantine",
        )

        # failed/ must have exactly one intake-* folder
        intake_dirs = [
            e for e in self._failed.iterdir()
            if e.is_dir() and e.name.startswith("intake-")
        ]
        self.assertEqual(len(intake_dirs), 1, "expected one intake-* dir in failed/")

        # A sibling .reason.txt must start with the quarantine prefix
        reason_files = [
            f for f in self._failed.iterdir()
            if f.name.endswith(".reason.txt")
        ]
        self.assertEqual(len(reason_files), 1, "expected one .reason.txt in failed/")
        reason_text = reason_files[0].read_text()
        self.assertTrue(
            reason_text.startswith("schema validation failed:"),
            f"Unexpected reason text: {reason_text!r}",
        )

        # _spawn_stage_runner must NOT have been called
        self.assertEqual(self._spawned, [])


if __name__ == "__main__":
    unittest.main()
