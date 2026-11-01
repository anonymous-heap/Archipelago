"""
Tests that filler created outside the item pool, for item links and fill fallbacks, is plain
filler: never progression, and never an item that exists only in Hard Mode.

    python -m pytest worlds/cvaos/test/test_filler.py -v
"""
from __future__ import annotations

import unittest

from BaseClasses import ItemClassification
from test.general import setup_multiworld

from .. import CVAOSWorld
from ..data import pickup_info_collection

HARD_MODE_ONLY = {pickup.display_name for pickup in pickup_info_collection if pickup.is_hard_mode_only}


class FillerTest(unittest.TestCase):
    def test_created_filler_is_never_progression_or_hard_mode_only(self):
        world = setup_multiworld(CVAOSWorld, ("generate_early",), seed=7).worlds[1]
        names = {world.get_filler_item_name() for _ in range(500)}
        self.assertGreater(len(names), 5)
        for name in names:
            with self.subTest(name=name):
                item = world.create_item(name)
                self.assertEqual(item.classification, ItemClassification.filler)
                self.assertNotIn(name, HARD_MODE_ONLY)


if __name__ == "__main__":
    unittest.main()
