"""
Tests for the soul table (soul_sources.py) that the spoiler prints.

Generation has no base ROM, so the spoiler runs the patch's own soul edits on a stand-in that holds
the vanilla soul bytes. Its table must equal the one read back from a patched ROM, for every soul
shuffle mode and rate option, on each base ROM.

    python -m pytest worlds/cvaos/test/test_soul_sources.py -v
"""
from __future__ import annotations

import io
import unittest
from random import Random

from ..rom import patch, soul_shuffle
from ..rom.enemy_table import TABLE_OFFSET, TABLE_SIZE, parse_table
from ..soul_sources import (planned_soul_table_lines, rom_soul_table_lines, soul_name,
                            vanilla_soul_name)
from . import base_roms
from .test_enemy_drop_rate import SCRIPTED_BOSSES, preset_seed
from .test_experimental_options import early


def config_for(mode: str, seed: int, **options) -> dict:
    """A rom_config.json for the soul edits, with a plan for ``mode`` drawn from ``seed``."""
    groups = patch.matched_groups() if mode == soul_shuffle.MODE_MATCHED else None
    plan = soul_shuffle.plan_shuffle(Random(seed), mode, groups)
    return {"soul_shuffle_plan": {str(target): source for target, source in plan.items()},
            "keep_soul_drop_rates": False, "shuffle_starting_soul": True,
            "multiply_soul_drop_rates": False, "soul_drop_rate_multiplier": 100, **options}


class SpoilerTest(unittest.TestCase):
    def test_the_spoiler_lists_what_the_patched_rom_holds(self):
        seed = preset_seed()
        if not seed.patched:
            self.skipTest("needs a USA Aria of Sorrow ROM")
        spoiler = io.StringIO()
        seed.world.write_spoiler(spoiler)
        boss_souls = {vanilla_soul_name(boss) for boss in SCRIPTED_BOSSES}
        for name, (_before, after) in seed.patched.items():
            lines = rom_soul_table_lines(after)
            with self.subTest(base=name):
                self.assertEqual(spoiler.getvalue(),
                                 f"\n\nSoul sources ({seed.world.player_name}):\n"
                                 + "".join(f"    {line}\n" for line in lines))
                # Live test 9 compares the host of a boss soul, so this seed must have one.
                rows = parse_table(after[TABLE_OFFSET:TABLE_OFFSET + TABLE_SIZE])
                hosts = [enemy_id for enemy_id, row in enumerate(rows)
                         if row.soul_index and enemy_id not in SCRIPTED_BOSSES
                         and soul_name(row.soul_type, row.soul_index) in boss_souls]
                self.assertTrue(hosts)
                self.assertTrue(all(rows[host].soul_rate for host in hosts))

    def test_a_seed_that_changes_no_soul_writes_no_table(self):
        spoiler = io.StringIO()
        early().write_spoiler(spoiler)
        self.assertEqual(spoiler.getvalue(), "")


class PlannedTableTest(unittest.TestCase):
    def test_the_planned_table_matches_the_patched_rom_for_every_option(self):
        bases = base_roms()
        if not bases:
            self.skipTest("needs a USA Aria of Sorrow ROM")
        drop_rates = {str(enemy_id): [350, 142] for enemy_id in range(0, 113, 3)}
        configs = {
            "within_type, rates move": config_for(soul_shuffle.MODE_WITHIN_TYPE, 1,
                                                  keep_soul_drop_rates=True,
                                                  multiply_soul_drop_rates=True,
                                                  soul_drop_rate_multiplier=400),
            "any_type": config_for(soul_shuffle.MODE_ANY_TYPE, 2),
            "matched with drop rates": config_for(soul_shuffle.MODE_MATCHED, 3,
                                                  enemy_drop_rates=drop_rates),
        }
        for base_name, base in bases.items():
            for name, config in configs.items():
                real = bytearray(base)
                patch.apply_soul_edits(real, config)
                patch.apply_drop_rate_edits(real, config)
                with self.subTest(base=base_name, config=name):
                    self.assertEqual(planned_soul_table_lines(config),
                                     rom_soul_table_lines(bytes(real)))


if __name__ == "__main__":
    unittest.main()
