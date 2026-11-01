"""
Tests that enemy regions exist only for enemies whose soul the player can farm.

Logic counts a soul as obtainable when the player can reach an enemy that drops it. An enemy that
never respawns after its first defeat gives only one chance at its soul, so it gets no region.

    python -m pytest worlds/cvaos/test/test_enemy_regions.py -v
"""
from __future__ import annotations

import unittest

from test.general import setup_multiworld

from .. import CVAOSWorld


class OneTimeEnemyTest(unittest.TestCase):
    def test_the_one_time_manticore_is_not_a_soul_source(self) -> None:
        multiworld = setup_multiworld(CVAOSWorld, ("generate_early", "create_regions"), seed=1)
        manticores = [region.name for region in multiworld.get_regions(1)
                      if region.name.startswith("Enemy: Manticore")]
        self.assertTrue(manticores, "the respawning Manticores must stay soul sources")
        self.assertFalse([name for name in manticores if "(106#132)" in name])


if __name__ == "__main__":
    unittest.main()
