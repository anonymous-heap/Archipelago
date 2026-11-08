"""
Tests for the soul ratings table (data/item_balancing/soul_tiers.csv).

Each soul's rating comes from the community soul tier lists, and its tier from cuts at 0.2, 0.4,
0.6 and 0.8. Soul Shuffle's matched value trades souls within groups of one color and one tier.

    python -m pytest worlds/cvaos/test/test_soul_tiers.py -v
"""
from __future__ import annotations

import unittest
from collections import Counter

from ..data.item_balancing import build_soul_tiers, soul_tiers
from ..data.item_info import by_category_id
from ..rom import soul_shuffle

_CATEGORY = {soul_shuffle.SOUL_TYPE_RED: "red_soul", soul_shuffle.SOUL_TYPE_BLUE: "blue_soul",
             soul_shuffle.SOUL_TYPE_YELLOW: "yellow_soul",
             soul_shuffle.SOUL_TYPE_ABILITY: "ability_soul"}


def enemy_soul(enemy_id: int) -> str:
    """Return the name of the soul that ``enemy_id`` drops in vanilla."""
    soul_type, index, _rate = soul_shuffle.VANILLA[enemy_id]
    return by_category_id[(_CATEGORY[soul_type], index - 1)].name


ENEMY_SOULS = [enemy_soul(eid) for eid, (_type, index, _rate) in soul_shuffle.VANILLA.items()
               if index != 0]


class SoulTiersTest(unittest.TestCase):
    def test_23_of_the_110_enemy_souls_are_strong(self):
        self.assertEqual(len(ENEMY_SOULS), 110)
        strong = [name for name in ENEMY_SOULS
                  if soul_tiers.soul_rating(name) >= soul_tiers.STRONG_RATING]
        self.assertEqual(len(strong), 23)
        self.assertTrue(all(soul_tiers.by_name[name].tier in ("A", "S") for name in strong))

    def test_tiers_are_cut_at_fifths_of_the_rating(self):
        for rating, tier in ((0.0, "D"), (0.1999, "D"), (0.2, "C"), (0.4, "B"), (0.5999, "B"),
                             (0.6, "A"), (0.8, "S"), (1.0, "S")):
            self.assertEqual(build_soul_tiers.tier_of(rating), tier)
        for row in soul_tiers.rows:
            self.assertEqual(row.tier, build_soul_tiers.tier_of(row.rating), row.soul)

    def test_matched_groups_by_color_and_tier(self):
        self.assertEqual(soul_tiers.matched_group("Winged Skeleton"), ("red", "B"))
        eligible = soul_shuffle.shuffleable_enemies(soul_shuffle.MODE_WITHIN_TYPE)
        groups = Counter(soul_tiers.matched_group(enemy_soul(eid)) for eid in eligible)
        # The Shuffle Starting Soul docstring says there are nine red souls in Winged Skeleton's
        # group.
        self.assertEqual(groups[("red", "B")], 9)

    def test_the_table_matches_its_build_script(self):
        built = build_soul_tiers.build()
        self.assertEqual([(row["soul"], row["rating"], row["tier"]) for row in built],
                         [(row.soul, row.rating, row.tier) for row in soul_tiers.rows])


if __name__ == "__main__":
    unittest.main()
