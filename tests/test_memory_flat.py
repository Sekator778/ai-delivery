"""Flat-file semantic store (backlog/T13 verdict).

600 MB of Qdrant and a second always-on service held 3.16 MB of vectors for
this collection. The verdict was to keep the semantics and drop the database:
the same recall over a JSONL file, ranked by a stdlib cosine scan. These tests
pin the behaviour that has to survive the swap — ranking, the scoped half of
recall, the score floor, the dilution cap — plus the property that matters most
until the operator flips it on: with the flag OFF, memory_inject still talks to
Qdrant and nothing about it changed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import memory_flat as flat  # noqa: E402
import memory_inject  # noqa: E402

TARGET = "/home/op/projects/orchestrator"
OTHER = "/home/op/projects/sandbox"


def _point(pid: str, vector: list[float], *, text: str = "t",
           target: str | None = None, kind: str = "task_lesson",
           timestamp: str = "2026-08-01T00:00:00+00:00") -> dict:
    payload = {"text": text, "kind": kind, "timestamp": timestamp}
    if target:
        payload["target_repo"] = target
    return {"id": pid, "vector": vector, "payload": payload}


def _store(points: list[dict]) -> Path:
    path = Path(tempfile.mkdtemp()) / "vectors.jsonl"
    path.write_text("".join(json.dumps(p) + "\n" for p in points))
    return path


def _using(path: Path, **extra):
    flat._CACHE.clear()
    return mock.patch.dict(
        os.environ, {flat.FLAG_ENV: "1", flat.PATH_ENV: str(path), **extra},
        clear=True)


class SearchTests(unittest.TestCase):
    def test_ranks_by_cosine_and_respects_the_limit(self) -> None:
        path = _store([
            _point("near", [1.0, 0.0, 0.0]),
            _point("mid", [0.7, 0.7, 0.0]),
            _point("far", [0.0, 1.0, 0.0]),
        ])
        with _using(path):
            hits = flat.search([1.0, 0.0, 0.0], limit=2, min_score=0.0)
        self.assertEqual([h["id"] for h in hits], ["near", "mid"])
        self.assertAlmostEqual(hits[0]["score"], 1.0, places=6)

    def test_magnitude_does_not_beat_direction(self) -> None:
        """A long vector pointing elsewhere must not outrank a short one
        pointing at the query — the reason both sides are normalised."""
        path = _store([_point("long-wrong", [0.0, 50.0, 0.0]),
                       _point("short-right", [0.01, 0.0, 0.0])])
        with _using(path):
            hits = flat.search([1.0, 0.0, 0.0], limit=1, min_score=0.0)
        self.assertEqual(hits[0]["id"], "short-right")

    def test_score_floor_drops_weak_matches(self) -> None:
        path = _store([_point("orthogonal", [0.0, 1.0, 0.0])])
        with _using(path):
            self.assertEqual(flat.search([1.0, 0.0, 0.0], 5, min_score=0.4), [])

    def test_scoped_half_filters_by_target(self) -> None:
        path = _store([
            _point("mine", [1.0, 0.0, 0.0], target=TARGET),
            _point("theirs", [1.0, 0.0, 0.0], target=OTHER),
            _point("prose", [1.0, 0.0, 0.0], kind="conversation"),
        ])
        with _using(path):
            scoped = flat.search([1.0, 0.0, 0.0], 5, TARGET, min_score=0.0)
            unscoped = flat.search([1.0, 0.0, 0.0], 5, None, min_score=0.0)
        self.assertEqual([h["id"] for h in scoped], ["mine"])
        self.assertEqual(len(unscoped), 3)

    def test_payload_only_rows_are_skipped_not_fatal(self) -> None:
        """The committed export carries no vectors; pointing the store at it by
        mistake must degrade to 'no hits', not to a crash."""
        path = _store([_point("ok", [1.0, 0.0])])
        with path.open("a") as fh:
            fh.write(json.dumps({"id": "novec", "payload": {"text": "x"}}) + "\n")
            fh.write("{not json\n")
        with _using(path):
            hits = flat.search([1.0, 0.0], 5, min_score=0.0)
        self.assertEqual([h["id"] for h in hits], ["ok"])

    def test_missing_store_is_loud_and_empty(self) -> None:
        missing = Path(tempfile.mkdtemp()) / "absent.jsonl"
        with _using(missing):
            self.assertEqual(flat.search([1.0, 0.0], 5), [])


class WriteTests(unittest.TestCase):
    def test_append_then_search_finds_it(self) -> None:
        path = _store([_point("old", [0.0, 1.0])])
        with _using(path):
            self.assertTrue(flat.append(_point("new", [1.0, 0.0], target=TARGET)))
            flat._CACHE.clear()
            hits = flat.search([1.0, 0.0], 5, TARGET, min_score=0.0)
        self.assertEqual([h["id"] for h in hits], ["new"])

    def test_retire_drops_the_oldest_over_the_cap(self) -> None:
        points = [_point(f"p{i}", [1.0, 0.0], target=TARGET,
                         timestamp=f"2026-08-0{i}T00:00:00+00:00")
                  for i in range(1, 5)]
        points.append(_point("other-target", [1.0, 0.0], target=OTHER))
        path = _store(points)
        with _using(path):
            dropped = flat.retire_over_cap(TARGET, cap=2)
            flat._CACHE.clear()
            kept = [json.loads(line)["id"]
                    for line in path.read_text().splitlines() if line.strip()]
        self.assertEqual(dropped, 2)
        self.assertEqual(kept, ["p3", "p4", "other-target"])  # other target untouched


class FlagTests(unittest.TestCase):
    def test_disabled_keeps_the_qdrant_path(self) -> None:
        """With the flag off, _search must go over HTTP exactly as before —
        the flat store is opt-in until the operator has dumped the vectors."""
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(memory_inject, "_post_json",
                                   return_value={"result": []}) as posted:
                with mock.patch.object(flat, "search") as searched:
                    memory_inject._search([1.0, 0.0], 5)
        self.assertTrue(posted.called)
        self.assertFalse(searched.called)

    def test_enabled_bypasses_qdrant_entirely(self) -> None:
        path = _store([_point("hit", [1.0, 0.0])])
        with _using(path):
            with mock.patch.object(memory_inject, "_post_json") as posted:
                hits = memory_inject._search([1.0, 0.0], 5)
        self.assertFalse(posted.called)          # no service contacted
        self.assertEqual([h["id"] for h in hits], ["hit"])


if __name__ == "__main__":
    unittest.main()
