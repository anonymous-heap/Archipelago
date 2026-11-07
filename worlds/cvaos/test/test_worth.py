"""
Tests for the item worth table (data/item_balancing/worth.csv).

Weapons, armor and restoratives take their depth on their class's ladder of vanilla pickups, and
weapons and armor take their tier-list desirability instead when it is higher. The anchors below
are the values the Experimental options design quotes.

    python -m pytest worlds/cvaos/test/test_worth.py -v
"""
from __future__ import annotations

import unittest

from ..data import item_info_collection
from ..data.item_balancing import build_worth, worth
from ..items import item_table

# Worth, as the design quotes it to two decimals.
WORTH_ANCHORS = {
    "Tasty Meat": 0.04, "Super Potion": 0.57, "Mind Up": 0.06, "Mana Prism": 0.30,
    "Iron Plate": 0.45, "Cutall": 0.45, "Battle Axe": 0.80, "Warhammer": 0.68,
    "Samurai Armor": 0.69, "Hrunting": 0.77,
}
# Depth on the item's class ladder, as the design quotes the sword and armor ladders.
LADDER_ANCHORS = {
    "Short Sword": 0.01, "Hrunting": 0.35, "Balmung": 0.71, "Leather Plate": 0.09, "Eversing": 0.61,
}


class WorthTableTest(unittest.TestCase):
    def test_the_design_anchors_hold(self):
        for name, expected in WORTH_ANCHORS.items():
            with self.subTest(name=name):
                self.assertAlmostEqual(worth.item_worth(name), expected, places=2)
        for name, expected in LADDER_ANCHORS.items():
            with self.subTest(ladder=name):
                self.assertAlmostEqual(worth.by_name[name].class_fit_depth, expected, places=2)

    def test_equipment_takes_its_desirability_when_that_is_higher(self):
        balmung = worth.by_name["Balmung"]
        self.assertGreater(balmung.desirability, balmung.class_fit_depth)
        self.assertEqual(balmung.worth, balmung.desirability)
        # A restorative keeps its ladder depth, whatever its desirability.
        potion = worth.by_name["High Potion"]
        self.assertGreater(potion.desirability, potion.class_fit_depth)
        self.assertEqual(potion.worth, potion.class_fit_depth)

    def test_the_three_unique_items_are_worth_one(self):
        for name in ("Valmanway", "Chaos Ring", "Soul Eater Ring"):
            with self.subTest(name=name):
                self.assertEqual(worth.item_worth(name), 1.0)

    def test_every_item_and_gold_amount_has_a_worth(self):
        souls = {"red_soul", "blue_soul", "yellow_soul", "ability_soul"}
        names = {info.name for info in item_info_collection if info.item_category not in souls}
        names |= {name for name in item_table if name.endswith(" Gold")}
        self.assertEqual(names, set(worth.by_name))

    def test_the_table_matches_its_build_script(self):
        built = build_worth.build()
        self.assertEqual([row["name"] for row in built], [row.name for row in worth.rows])
        for row in built:
            with self.subTest(name=row["name"]):
                self.assertEqual(round(row["worth"], build_worth.DECIMALS),
                                 worth.item_worth(row["name"]))


if __name__ == "__main__":
    unittest.main()
