"""Docs regression tests for scripts/publish-public.sh.

Asserts:
- ops/SCRIPTS-README.md has a row for publish-public.sh (AC-13)
- CHANGELOG.md [Unreleased] has a bullet naming scripts/publish-public.sh (AC-14)
- .gitignore lists ops/publish-blocklist.local (FR-010)
- ops/publish-blocklist.local.example is tracked and has no real markers (FR-009)
- gitleaks dir over the repo reports 0 findings (self-poisoning guard, E9)
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_scripts_readme_has_publish_row() -> None:
    """AC-13: ops/SCRIPTS-README.md must contain a row for publish-public.sh."""
    text = (REPO_ROOT / "ops" / "SCRIPTS-README.md").read_text(encoding="utf-8")
    assert "publish-public.sh" in text, (
        "ops/SCRIPTS-README.md does not contain a row for publish-public.sh"
    )


def test_changelog_unreleased_has_publish_bullet() -> None:
    """AC-14: CHANGELOG.md [Unreleased] section contains a bullet for scripts/publish-public.sh."""
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased_start = text.find("## [Unreleased]")
    assert unreleased_start != -1, "No [Unreleased] section found in CHANGELOG.md"
    next_section = text.find("\n## ", unreleased_start + 1)
    unreleased = (
        text[unreleased_start:next_section]
        if next_section != -1
        else text[unreleased_start:]
    )
    assert "scripts/publish-public.sh" in unreleased, (
        "CHANGELOG.md [Unreleased] section does not contain a bullet naming "
        "scripts/publish-public.sh"
    )


def test_gitignore_lists_blocklist() -> None:
    """FR-010: .gitignore must list ops/publish-blocklist.local."""
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "ops/publish-blocklist.local" in text, (
        ".gitignore does not list ops/publish-blocklist.local"
    )


def test_blocklist_example_exists_and_is_tracked() -> None:
    """FR-009: ops/publish-blocklist.local.example must be tracked in git."""
    example = REPO_ROOT / "ops" / "publish-blocklist.local.example"
    assert example.exists(), "ops/publish-blocklist.local.example does not exist on disk"
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(example)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "ops/publish-blocklist.local.example is not tracked in git"
    )


def test_blocklist_example_has_no_real_markers() -> None:
    """FR-009: The example must contain only placeholder values, not real operator data."""
    text = (REPO_ROOT / "ops" / "publish-blocklist.local.example").read_text(
        encoding="utf-8"
    )
    # No real operator-identifying values
    assert "sekator" not in text.lower(), "Example contains operator username"
    assert "gmail.com" not in text.lower(), "Example contains real email domain"
    # No literal real LAN IP addresses (placeholder regex patterns are fine,
    # but actual IPs like 192.168.1.5 are not)
    assert "192.168.1." not in text, "Example contains real IP address"
    assert "10.0.0." not in text, "Example contains real IP address"


def test_no_gitleaks_findings_in_repo() -> None:
    """E9 self-poisoning guard: gitleaks dir must report 0 findings on the repo."""
    gitleaks_bin = shutil.which("gitleaks") or "/opt/homebrew/bin/gitleaks"
    if not pathlib.Path(gitleaks_bin).exists():
        import pytest  # type: ignore[import-untyped]
        pytest.skip("gitleaks not found")
    result = subprocess.run(
        [gitleaks_bin, "dir", str(REPO_ROOT), "--no-banner", "--exit-code", "1"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"gitleaks found secrets in the repo — self-poisoning guard E9 failed.\n"
        f"stdout: {result.stdout[:2000]}\nstderr: {result.stderr[:500]}"
    )
