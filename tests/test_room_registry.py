"""T32 — rooms run side by side under a cap; the registry is the testable half."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import room_driver as driver  # noqa: E402


class RegistryTests(unittest.TestCase):
    def test_cap_refuses_the_extra_room_and_frees_on_release(self) -> None:
        reg = driver.RoomRegistry(2)
        self.assertTrue(reg.acquire("room-a"))
        self.assertTrue(reg.acquire("room-b"))
        self.assertFalse(reg.acquire("room-c"))
        self.assertEqual(len(reg), 2)
        reg.release("room-a")
        self.assertTrue(reg.acquire("room-c"))

    def test_release_of_unknown_room_is_harmless(self) -> None:
        reg = driver.RoomRegistry(1)
        reg.release("never-started")
        self.assertEqual(len(reg), 0)

    def test_limit_never_below_one(self) -> None:
        self.assertEqual(driver.RoomRegistry(0).limit, 1)
        self.assertEqual(driver.max_concurrent_rooms({driver.ROOM_MAX_CONCURRENT_ENV: "0"}), 1)

    def test_env_parsing(self) -> None:
        self.assertEqual(driver.max_concurrent_rooms({}), driver.DEFAULT_MAX_CONCURRENT)
        self.assertEqual(driver.max_concurrent_rooms({driver.ROOM_MAX_CONCURRENT_ENV: "7"}), 7)
        self.assertEqual(driver.max_concurrent_rooms({driver.ROOM_MAX_CONCURRENT_ENV: "junk"}),
                         driver.DEFAULT_MAX_CONCURRENT)


if __name__ == "__main__":
    unittest.main()
