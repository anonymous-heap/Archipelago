"""
Tests for each enemy type's HP and drop rarity bytes (data/enemy_info/enemy_stats.csv).

    python -m pytest worlds/cvaos/test/test_enemy_stats.py -v
"""
from __future__ import annotations

import unittest

from . import find_rom
from ..data import enemy_type_by_name, enemy_type_collection
from ..data.enemy_info import build_enemy_stats


class EnemyStatsTest(unittest.TestCase):
    def test_known_values_hold(self):
        # Iron Golem's 9999 HP is well known, and the design's live test of the rarity byte
        # starts from Zombie's vanilla bytes.
        self.assertEqual(enemy_type_by_name["Iron Golem"].hp, 9999)
        self.assertEqual(enemy_type_by_name["Zombie"].drop_rarities, (20, 30))

    def test_the_table_matches_the_rom(self):
        path = find_rom()
        if path is None:
            self.skipTest("base ROM not found (set CVAOS_USA_ROM)")
        with open(path, "rb") as fh:
            rows = build_enemy_stats.read_rows(fh.read())
        self.assertEqual([(enemy.hp, enemy.drop_rarities) for enemy in enemy_type_collection],
                         [(row.hp, (row.drop_1_rarity, row.drop_2_rarity)) for row in rows])


if __name__ == "__main__":
    unittest.main()
