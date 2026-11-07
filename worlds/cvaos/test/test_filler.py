"""
Tests that filler created outside the item pool, for item links and fill fallbacks, is plain
filler: never progression, never an item that exists only in Hard Mode, and only consumables and
gold that are no better than what the castle's pickups offer a quarter of the way in.

    python -m pytest worlds/cvaos/test/test_filler.py -v
"""
from __future__ import annotations

import unittest

from BaseClasses import ItemClassification
from test.general import setup_multiworld

from .. import CVAOSWorld
from ..data import pickup_info_collection
from ..data.item_balancing import worth

HARD_MODE_ONLY = {pickup.item_name for pickup in pickup_info_collection if pickup.is_hard_mode_only}


class FillerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.world = setup_multiworld(CVAOSWorld, ("generate_early",), seed=7).worlds[1]
        self.names = {self.world.get_filler_item_name() for _ in range(500)}

    def test_created_filler_is_never_progression_or_hard_mode_only(self):
        self.assertGreater(len(self.names), 5)
        for name in self.names:
            with self.subTest(name=name):
                item = self.world.create_item(name)
                self.assertEqual(item.classification, ItemClassification.filler)
                self.assertNotIn(name, HARD_MODE_ONLY)

    def test_created_filler_is_a_cheap_consumable_or_gold(self):
        for name in self.names:
            with self.subTest(name=name):
                self.assertIn(worth.by_name[name].category, ("consumable", "money"))
                self.assertLessEqual(worth.item_worth(name), worth.CHEAP_WORTH)


if __name__ == "__main__":
    unittest.main()
