"""
Item names: every copy of an item shares its name and code, and a location name says where a copy
is. Ids that older versions gave to each copy still name the same items, and a YAML that names a
copy the way older versions did still generates.

    python -m pytest worlds/cvaos/test/test_item_names.py -v
"""
from __future__ import annotations

import json
import os
import re
import unittest
from argparse import Namespace
from collections import Counter

import Main
from BaseClasses import ItemClassification, MultiWorld, PlandoOptions
from Generate import roll_settings
from test.general import setup_multiworld
from worlds import network_data_package
from worlds.AutoWorld import data_package_checksum

from .. import CVAOSWorld
from ..data import pickup_info_collection
from ..item_granting import canonical_code
from ..item_smoothing import PARAMS_BY_KEY, _assign_targets
from ..items import (CHECK_ITEM_CHOICES, ENEMY_DROP, RETIRED_COPY_NAMES, CVAOSItem,
                     create_enemy_item, item_name_groups, item_table)

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


def generate_from_yamls(yamls: list[dict], seed: int) -> MultiWorld:
    """Roll each YAML through Generate's option verification, then generate the multiworld with
    Main.main, without balancing or output, and return it."""
    plando = PlandoOptions.from_set({"items"})
    args = Namespace(multi=len(yamls), race=False, outputname=None, outputpath=None, spoiler=0,
                     plando=plando, csv_output=False, skip_output=True, spoiler_only=False,
                     skip_prog_balancing=True, sprite={}, sprite_pool={})
    for player, yaml in enumerate(yamls, 1):
        for key, value in vars(roll_settings(yaml, plando)).items():
            if not hasattr(args, key):
                setattr(args, key, {})
            getattr(args, key)[player] = value
        args.sprite[player] = None
        args.sprite_pool[player] = []
    return Main.main(args, seed)


def retired_name_yaml(name: str, **options) -> dict:
    """Return a YAML with ``options`` and an item link of 100 Gold, which it names the way older
    versions did."""
    link = {"name": "Gold Link", "item_pool": ["100 Gold (A)", "500 Gold (B)"],
            "exclude": ["500 Gold (C)"], "replacement_item": None}
    return {"name": name, "game": CVAOSWorld.game,
            CVAOSWorld.game: {"item_links": [link], **options}}


class RetiredNameTest(unittest.TestCase):
    def test_each_retired_copy_name_is_a_group_holding_its_item(self):
        retired = {name for name in OLD_IDS if COPY_SUFFIX.search(name)}
        self.assertEqual(set(RETIRED_COPY_NAMES), retired)
        for name in retired:
            with self.subTest(name=name):
                self.assertEqual(CVAOSWorld.item_name_groups[name], {COPY_SUFFIX.sub("", name)})

    def test_the_data_package_leaves_out_the_retired_names(self):
        package = dict(network_data_package["games"][CVAOSWorld.game])
        self.assertEqual(set(package["item_name_groups"]) & set(RETIRED_COPY_NAMES), set())
        self.assertIn("Gold", package["item_name_groups"])
        checksum = package.pop("checksum")
        self.assertEqual(data_package_checksum(package), checksum)

    def test_a_yaml_that_names_retired_copies_generates(self):
        first = "White Dragon Hallway [00D Potion (A)]"
        second = "Secret Room behind Peeping Eye [00B Mind Up (A)]"
        plando = [{"items": ["Super Potion (A)"], "location": first},
                  {"items": {"Mind Up (B)": 1}, "location": second}]
        yamls = [retired_name_yaml("Old", local_items=["Potion (A)"],
                                   non_local_items=["High Potion (B)"],
                                   start_hints=["Mana Prism (C)"], plando_items=plando),
                 retired_name_yaml("New")]
        multiworld = generate_from_yamls(yamls, seed=5)
        self.assertEqual(multiworld.worlds[1].options.start_hints.value, {"Mana Prism"})
        (group,) = multiworld.groups.values()
        self.assertEqual(set(group["item_pool"]), {"100 Gold"})

        def worlds_holding(name: str) -> set[int]:
            return {location.player for location in multiworld.get_filled_locations()
                    if location.item.player == 1 and location.item.name == name}

        self.assertEqual(worlds_holding("Potion"), {1})
        self.assertEqual(worlds_holding("High Potion"), {2})
        self.assertEqual(multiworld.get_location(first, 1).item.name, "Super Potion")
        self.assertEqual(multiworld.get_location(second, 1).item.name, "Mind Up")


class PoolTest(unittest.TestCase):
    def test_an_enemy_check_adds_another_copy_of_its_item(self):
        without_checks = pool()
        with_checks = pool(enemy_checks="all")
        # Three pickups hold a Mind Up, and four enemy types drop one.
        self.assertEqual(with_checks["Mind Up"] - without_checks["Mind Up"], 4)
        self.assertEqual(with_checks["Mind Up"], 7)
        # No pickup holds a Cloth Tunic, and two enemy types drop one.
        self.assertEqual(with_checks["Cloth Tunic"], 2)
        self.assertEqual(with_checks["100 Gold"] - without_checks["100 Gold"], 43)

    def test_an_enemy_copy_never_holds_progression_but_other_copies_keep_theirs(self):
        world = setup_multiworld(CVAOSWorld, ("generate_early",), seed=1,
                                 options={"enemy_checks": "all"}).worlds[1]
        progression = [name for name in CHECK_ITEM_CHOICES
                       if item_table[name].classification & ItemClassification.progression]
        self.assertTrue(progression)
        for name in CHECK_ITEM_CHOICES:
            with self.subTest(item=name):
                self.assertFalse(create_enemy_item(world, name).advancement)
        for name in progression:
            with self.subTest(item=name):
                self.assertTrue(world.create_item(name).advancement)

    def test_an_enemy_drop_follows_the_rom_table(self):
        # The table stores each drop as its item index plus 1, and the game subtracts the 1. These
        # four drops are well known, so an off-by-one in enemy_types.csv shows up here.
        expected = {"Dryad": "Mystletain", "Flame Demon": "Laevatain", "Poison Worm": "Anti-Venom",
                    "Arachne": "Potion"}
        self.assertEqual({enemy: ENEMY_DROP[enemy] for enemy in expected}, expected)


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
