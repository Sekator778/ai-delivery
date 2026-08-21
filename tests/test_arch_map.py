"""Suite gate for docs/CALL-TREE.md — the recorded architecture map.

Runs `ops/check-arch-map.py --check`, which extracts the call-graph facts
(spawn sites, dispatcher imports, stage→persona dispatch, hooks, personas)
from the code and diffs them against the fact block embedded in the document.

This is the enforcement half of the doc's contract: a commit that changes the
topology fails the suite until it also runs `--update` — putting the author
inside the document, next to the prose their change may have falsified. See
docs/CALL-TREE.md "Why this file exists" for the drift incident that made
this necessary.
"""

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "ops" / "check-arch-map.py"


class ArchMapSyncTests(unittest.TestCase):
    def test_call_tree_doc_matches_the_code(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(CHECKER), "--check"],
            capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT),
        )
        self.assertEqual(
            proc.returncode, 0,
            "docs/CALL-TREE.md is out of sync with the code:\n"
            f"{proc.stdout}\n{proc.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
