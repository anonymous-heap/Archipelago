"""
The annotator's test runs: what a set of runs settles about a pair, and the test ROM it runs on.

The ROM tests read the USA ROM, and skip when it is not found.

    python -m pytest worlds/cvaos/test/test_annotator_testing.py -v
"""
from __future__ import annotations

import os
import tempfile
import unittest

from ..tools.annotator import emulator, testing
from . import find_rom

DOOR, ENEMY = "104:108", 117


def entry(used: list[str], outcome: str, door: str = DOOR, enemy: int = ENEMY) -> dict:
    return {"door": door, "enemy": enemy, "used": used, "outcome": outcome}


class TestSummarize(unittest.TestCase):
    def summary(self, *entries: dict) -> dict:
        return testing.summarize(list(entries))[f"{DOOR}|{ENEMY}"]

    def test_ways_are_the_minimal_successes(self):
        summary = self.summary(entry(["DJump", "Glide"], "reached"), entry(["DJump"], "defeated"),
                               entry(["Bat"], "reached"), entry(["Bat", "DJump"], "reached"))
        self.assertEqual(summary["ways"], [["Bat"], ["DJump"]])
        self.assertEqual(summary["successes"], 4)

    def test_a_failure_settles_its_subsets(self):
        summary = self.summary(entry(["DJump", "Glide"], "failed"))
        self.assertEqual(summary["ways"], [])
        for settled in ([], ["DJump"], ["Glide"], ["DJump", "Glide"]):
            self.assertNotIn(settled, summary["next"])
        self.assertIn(["Slide"], summary["next"])

    def test_a_success_settles_its_supersets(self):
        summary = self.summary(entry(["Slide"], "reached"), entry([], "failed"))
        self.assertNotIn(["Slide"], summary["next"])
        self.assertNotIn(["Glide", "Slide"], summary["next"])
        self.assertIn(["Glide"], summary["next"])

    def test_a_success_with_no_souls_settles_everything(self):
        summary = self.summary(entry([], "reached"))
        self.assertEqual(summary["ways"], [[]])
        self.assertEqual(summary["next"], [])

    def test_next_lists_at_most_the_limit_smallest_first(self):
        summary = testing.summarize([entry([], "failed")], limit=3)[f"{DOOR}|{ENEMY}"]
        self.assertEqual(summary["next"], [["Glide"], ["Slide"], ["DJump"]])

    def test_a_success_inside_a_failure_is_a_contradiction(self):
        summary = self.summary(entry(["DJump"], "reached"), entry(["DJump", "Glide"], "failed"))
        self.assertEqual(summary["contradictions"], [["DJump"]])

    def test_pairs_are_kept_apart(self):
        summaries = testing.summarize([entry([], "reached"), entry([], "failed", door="104:020")])
        self.assertEqual(summaries[f"{DOOR}|{ENEMY}"]["ways"], [[]])
        self.assertEqual(summaries[f"104:020|{ENEMY}"]["ways"], [])


class TestAttemptLog(unittest.TestCase):
    def test_runs_round_trip_through_the_log(self):
        run = testing.Run(ENEMY, "Ghost", "104", DOOR, (512, 128), 7, ["Bat"],
                          used={"Bat", "DJump"}, outcome="reached")
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(testing.load(folder), [])
            testing.append(folder, run, "tester")
            testing.append(folder, run, "tester")
            entries = testing.load(folder)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["used"], ["Bat", "DJump"])
        self.assertEqual(entries[0]["by"], "tester")
        self.assertEqual(testing.summarize(entries)[f"{DOOR}|{ENEMY}"]["ways"], [["Bat", "DJump"]])


class TestTestRom(unittest.TestCase):
    def setUp(self):
        path = find_rom()
        if path is None:
            raise unittest.SkipTest("USA ROM not available")
        with open(path, "rb") as handle:
            self.rom = handle.read()
        self.path = path

    def test_boot_patch_marks_the_opening_event_seen(self):
        built = emulator.build_test_rom(self.rom)
        offset = emulator.BOOT_ADDRESS - emulator.ROM_BASE
        self.assertEqual(built[offset:offset + len(emulator.BOOT_PATCH)], emulator.BOOT_PATCH)
        # After storing the room pointer at 0x020003CC, the patch steps back 0x8C to 0x02000340
        # and sets bit 0 there: subs r1, #0x8C; ldrb; movs r2, #1; orrs; strb.
        self.assertIn(bytes.fromhex("8c390878012210430870"), emulator.BOOT_PATCH)

    def test_only_the_us_rom_is_accepted(self):
        with self.assertRaises(ValueError):
            emulator.build_test_rom(self.rom[:-1] + bytes([self.rom[-1] ^ 0xFF]))

    def test_room_entity_id_finds_the_placement_entry(self):
        room_006 = 0x0850F32C
        skeleton = 2
        self.assertEqual(emulator.room_entity_id(self.rom, room_006, skeleton, 128, 176), 2)
        self.assertEqual(emulator.room_entity_id(self.rom, room_006, skeleton, 640, 176), 8)
        self.assertIsNone(emulator.room_entity_id(self.rom, room_006, skeleton, 129, 176))
        self.assertIsNone(emulator.room_entity_id(self.rom, room_006, skeleton + 1, 128, 176))

    def test_test_rom_is_named_by_its_content(self):
        with tempfile.TemporaryDirectory() as folder:
            path = emulator.test_rom(self.path, folder)
            self.assertTrue(os.path.exists(path))
            self.assertEqual(emulator.test_rom(self.path, folder), path)
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), emulator.build_test_rom(self.rom))


if __name__ == "__main__":
    unittest.main()
