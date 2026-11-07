"""
Tests for the drop table (drop_sources.py) that the spoiler prints.

Generation has no base ROM, so the spoiler runs the patch's own drop edits on a stand-in that holds
the vanilla drop fields. Its table must equal the one read back from the patched ROM, on each base
ROM.

    python -m pytest worlds/cvaos/test/test_drop_sources.py -v
"""
from __future__ import annotations

import unittest

from ..drop_sources import planned_drop_table_lines, rom_drop_table_lines
from .test_enemy_drop_rate import preset_seed
from .test_experimental_options import early
from .test_soul_sources import spoiler_sections


class SpoilerTest(unittest.TestCase):
    def test_the_spoiler_lists_what_the_patched_rom_holds(self):
        seed = preset_seed()
        if not seed.patched:
            self.skipTest("needs a USA Aria of Sorrow ROM")
        sections = spoiler_sections(seed.world)
        for name, (_before, after) in seed.patched.items():
            with self.subTest(base=name):
                self.assertEqual(sections["Item drops"], rom_drop_table_lines(after))

    def test_a_seed_that_changes_no_drop_writes_no_table(self):
        self.assertNotIn("Item drops", spoiler_sections(early(soul_shuffle="within_type")))

    def test_each_line_names_both_drops_and_their_chances(self):
        # Zombie's vanilla rarities are 20 and 30. The roll picks the common slot three times in
        # four, and then drops it with chance 4 / (20*4 + 16).
        lines = planned_drop_table_lines({})
        self.assertIn(
            "Zombie: Cloth Tunic (common, 1 in 32 kills), Baselard (rare, 1 in 136 kills)", lines)
        self.assertIn("Axe Armor: Leather Plate (common, 1 in 32 kills)", lines)


if __name__ == "__main__":
    unittest.main()
