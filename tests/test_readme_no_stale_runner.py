"""Tests that README.md does not contain the stale claude-runner.sh reference.

FR-001 / AC-001: The meta/ row in the architecture table must not list
'claude-runner.sh'; the script does not exist in the repo. The row itself
must still be present so the meta/ component remains documented.
"""

import pathlib

README = pathlib.Path(__file__).parent.parent / "README.md"


def _readme_text() -> str:
    return README.read_text(encoding="utf-8")


def test_stale_runner_reference_absent():
    """AC-001: 'claude-runner.sh' must not appear anywhere in README.md."""
    text = _readme_text()
    assert "claude-runner.sh" not in text, (
        "README.md still contains the stale 'claude-runner.sh' reference; "
        "remove it from the meta/ row in the architecture table."
    )


def test_meta_row_still_present():
    """AC-001 (no-regression): the meta/ row must still exist in the table."""
    text = _readme_text()
    assert "| `meta/`" in text, (
        "README.md no longer contains the meta/ row — the whole row was "
        "accidentally removed; it must remain with an accurate description."
    )
