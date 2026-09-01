"""`qdrant-memory.py dump --with-vectors` must produce the flat store (T17).

The first cut of that flag (PR #31) scrolled the vectors and then dropped them
while building records, and always wrote the payload-only filename — so the
"reproducible migration path" it advertised did not exist, and the live store
had to be written by a one-off script. These tests pin the contract
`dispatcher/memory_flat.py` actually depends on: the shape
{"id", "vector", "payload"}, and a default destination that does not overwrite
the committed payload-only export.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import memory_flat as flat  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "qdrant_memory", REPO_ROOT / "scripts" / "qdrant-memory.py")
qm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qm)


POINTS = [
    {"id": "b", "vector": [0.0, 1.0], "payload": {"text": "second", "kind": "note"}},
    {"id": "a", "vector": [1.0, 0.0], "payload": {"text": "first", "kind": "note"}},
]


def _args(out: Path | None, with_vectors: bool) -> argparse.Namespace:
    return argparse.Namespace(collection="meta_agent_mem",
                              out=str(out) if out else None,
                              with_vectors=with_vectors,
                              exclude_flagged=False)


class VectorDumpTests(unittest.TestCase):
    def _dump(self, out: Path | None, with_vectors: bool, points=None):
        with mock.patch.object(qm, "http_json", return_value={"result": {"points_count": 2}}), \
             mock.patch.object(qm, "scroll_all", return_value=list(points or POINTS)), \
             mock.patch.object(qm, "gate", return_value={}):
            qm.cmd_dump(_args(out, with_vectors))

    def test_vectors_survive_into_the_file(self) -> None:
        out = Path(tempfile.mkdtemp()) / "vectors.jsonl"
        self._dump(out, True)
        rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
        self.assertEqual([r["id"] for r in rows], ["a", "b"])       # sorted by id
        self.assertEqual(rows[0]["vector"], [1.0, 0.0])
        self.assertEqual(rows[0]["payload"]["text"], "first")

    def test_memory_flat_can_load_what_the_dump_writes(self) -> None:
        """The actual acceptance: the file the dump produces is a store."""
        out = Path(tempfile.mkdtemp()) / "vectors.jsonl"
        self._dump(out, True)
        flat._CACHE.clear()
        with mock.patch.dict("os.environ", {flat.FLAG_ENV: "1",
                                            flat.PATH_ENV: str(out)}, clear=True):
            self.assertEqual(len(flat.load()), 2)
            hits = flat.search([1.0, 0.0], limit=1, min_score=0.0)
        self.assertEqual(hits[0]["id"], "a")

    def test_payload_only_dump_is_unchanged(self) -> None:
        out = Path(tempfile.mkdtemp()) / "payload.jsonl"
        self._dump(out, False)
        rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
        self.assertNotIn("vector", rows[0])
        self.assertEqual(sorted(rows[0]), ["id", "payload"])

    def test_defaults_do_not_collide(self) -> None:
        """--with-vectors must not default to the committed payload export."""
        self.assertNotEqual(qm.DEFAULT_EXPORT, qm.DEFAULT_VECTOR_EXPORT)
        self.assertEqual(qm.DEFAULT_VECTOR_EXPORT.name,
                         "meta_agent_mem.vectors.jsonl")

    def test_vectorless_points_are_dropped_not_written(self) -> None:
        """memory_flat skips rows without a vector, so writing them would give
        a store that silently holds fewer points than the file shows."""
        points = [*POINTS, {"id": "c", "payload": {"text": "no vector"}}]
        out = Path(tempfile.mkdtemp()) / "vectors.jsonl"
        self._dump(out, True, points)
        rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
        self.assertEqual([r["id"] for r in rows], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
