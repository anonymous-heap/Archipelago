"""
Item names: every copy of an item shares its name and code, and a location name says where a copy
is. Ids that older versions gave to each copy still name the same items.

    python -m pytest worlds/cvaos/test/test_item_names.py -v
"""
from __future__ import annotations

import json
import os
import re
import unittest
from collections import Counter

from BaseClasses import ItemClassification
from test.general import setup_multiworld

from .. import CVAOSWorld
from ..data import pickup_info_collection
from ..item_granting import canonical_code
from ..item_smoothing import PARAMS_BY_KEY, _assign_targets
from ..items import ENEMY_ITEM, CVAOSItem, create_enemy_item, item_name_groups, item_table

# The item ids of the last version that named each copy of an item, such as "Potion (A)".
with open(os.path.join(os.path.dirname(__file__), "item_ids_with_copy_names.json")) as _file:
    OLD_IDS: dict[str, int] = json.load(_file)
COPY_SUFFIX = re.compile(r" \([A-Z]\)$")


def pool(**options) -> Counter:
    multiworld = setup_multiworld(CVAOSWorld, ("generate_early", "create_regions", "create_items"),
                                  seed=1, options=options)
    items = [*multiworld.itempool, *multiworld.worlds[1].get_pre_fill_items()]
    return Counter(item.name for item in items)


class NameTest(unittest.TestCase):
    def test_no_item_name_says_which_copy_it_is(self):
        self.assertEqual([name for name in item_table if name.endswith(")")], [])

    def test_each_item_has_its_own_code_with_the_retired_field_clear(self):
        codes = [data.code for data in item_table.values()]
        self.assertEqual(len(set(codes)), len(codes))
        self.assertEqual([code for code in codes if canonical_code(code) != code], [])

    def test_the_pickups_holding_an_item_all_hold_the_same_one(self):
        for pickup in pickup_info_collection:
            with self.subTest(pickup=pickup.display_name):
                self.assertEqual(COPY_SUFFIX.sub("", pickup.display_name), pickup.item_name)

    def test_every_old_id_still_names_the_same_item(self):
        # A copy's old id, with the retired field cleared, is the current id of its item. The first
        # copy of each item, and every item with one copy, keeps its id as it was.
        by_code = {data.code: name for name, data in item_table.items()}
        for old_name, old_id in OLD_IDS.items():
            with self.subTest(item=old_name):
                self.assertEqual(by_code.get(canonical_code(old_id)), COPY_SUFFIX.sub("", old_name))
        kept = {name for name, old_id in OLD_IDS.items() if old_id in by_code}
        first_copies = {name for name in OLD_IDS
                        if not COPY_SUFFIX.search(name) or name.endswith(" (A)")}
        self.assertEqual(kept, first_copies)

    def test_the_gold_group_holds_every_gold_amount(self):
        self.assertEqual(item_name_groups["Gold"],
                         {"100 Gold", "500 Gold", "1000 Gold", "2000 Gold"})

    def test_a_hint_for_an_item_names_the_item_itself(self):
        # The server hints every copy of the item a hint names. A hint that matches an item or
        # group name exactly always resolves to that name, so each plain name only needs to exist.
        names = CVAOSWorld.item_names | set(CVAOSWorld.item_name_groups)
        for query in ("High Mind Up", "Potion", "100 Gold", "Death's Sickle", "Gold"):
            with self.subTest(query=query):
                self.assertIn(query, names)


class PoolTest(unittest.TestCase):
    def test_an_enemy_check_adds_another_copy_of_its_item(self):
        without_checks = pool()
        with_checks = pool(enemy_checks="all")
        self.assertEqual(with_checks["High Mind Up"] - without_checks["High Mind Up"], 4)
        self.assertEqual(with_checks["High Mind Up"], 8)
        self.assertEqual(with_checks["Gym Clothes"], 2)
        self.assertEqual(with_checks["100 Gold"] - without_checks["100 Gold"], 43)

    def test_an_enemy_copy_never_holds_progression_but_other_copies_keep_theirs(self):
        world = setup_multiworld(CVAOSWorld, ("generate_early",), seed=1,
                                 options={"enemy_checks": "all"}).worlds[1]
        progression = [name for name in set(ENEMY_ITEM.values())
                       if item_table[name].classification & ItemClassification.progression]
        self.assertTrue(progression)
        for enemy_name, name in ENEMY_ITEM.items():
            with self.subTest(enemy=enemy_name):
                self.assertFalse(create_enemy_item(world, enemy_name).advancement)
        for name in progression:
            with self.subTest(item=name):
                self.assertTrue(world.create_item(name).advancement)

    def test_death_s_sickle_comes_from_enemies_even_without_hard_mode_pickups(self):
        self.assertEqual(pool(enemy_checks="all")["Death's Sickle"], 2)
        self.assertEqual(pool(enemy_checks="all", hard_mode_pickups="always")["Death's Sickle"], 3)


class SmoothingTest(unittest.TestCase):
    def test_each_copy_of_an_item_gets_its_own_target(self):
        # Items compare equal by name and player, so targets kept by item would merge the copies.
        import random
        items = [CVAOSItem(name, item_table[name].classification, item_table[name].code, 1)
                 for name in ["100 Gold"] * 10 + ["High Potion"] * 5 + ["Potion"] * 4]
        targets = _assign_targets(items, PARAMS_BY_KEY["strict"], random.Random(3))
        self.assertEqual(len(targets), len(items))
        self.assertEqual(len(set(targets)), len(items))


if __name__ == "__main__":
    unittest.main()
