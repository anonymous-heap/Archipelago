"""
Tests that filler created outside the item pool, for item links and fill fallbacks, is plain
filler: never progression, never an item that exists only in Hard Mode, and only consumables and
gold that are no better than what the castle's pickups offer a quarter of the way in. Filler also
never takes a consumable past the copies the game lets Soma carry.

    python -m pytest worlds/cvaos/test/test_filler.py -v
"""
from __future__ import annotations

import unittest
from argparse import Namespace
from collections import Counter

from BaseClasses import CollectionState, ItemClassification, MultiWorld
from Options import ItemLinks
from test.general import setup_multiworld
from worlds.AutoWorld import call_all

from .. import CVAOSWorld
from ..data import pickup_info_collection
from ..data.item_balancing import worth
from ..items import CONSUMABLE_CAP, item_name_groups
from . import PRESET

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


def linked_multiworld(seed: int) -> MultiWorld:
    """Return two players on the Experimental preset who link their weapons, after core links
    the items.

    Core takes each player's weapons out of the pool and pads each pool with filler in their place.
    """
    multiworld = MultiWorld(2)
    multiworld.game = {player: CVAOSWorld.game for player in multiworld.player_ids}
    multiworld.player_name = {player: f"Linker{player}" for player in multiworld.player_ids}
    multiworld.set_seed(seed)
    args = Namespace()
    for name, option in CVAOSWorld.options_dataclass.type_hints.items():
        setattr(args, name, {player: option.from_any(PRESET.get(name, option.default))
                             for player in multiworld.player_ids})
    link = [{"name": "Weapons Link", "item_pool": ["Weapons"], "link_replacement": False,
             "replacement_item": None}]
    args.item_links = {player: ItemLinks.from_any(link) for player in multiworld.player_ids}
    multiworld.set_options(args)
    multiworld.set_item_links()
    multiworld.state = CollectionState(multiworld)
    for step in ("generate_early", "create_regions", "create_items"):
        call_all(multiworld, step)
    multiworld.link_items()
    return multiworld


class ConsumableCapTest(unittest.TestCase):
    def test_item_link_filler_keeps_each_consumable_within_the_cap(self):
        # Without the cap, this seed's filler takes player 1 to 11 Potions and player 2 to 11
        # High Potions.
        multiworld = linked_multiworld(4242)
        for player in multiworld.player_ids:
            world = multiworld.worlds[player]
            items = [*multiworld.itempool, *world.get_pre_fill_items()]
            held = Counter(item.name for item in items if item.player == player)
            over = {name: count for name, count in held.items()
                    if name in item_name_groups["Consumables"] and count > CONSUMABLE_CAP}
            self.assertEqual(over, {}, f"player {player}")


if __name__ == "__main__":
    unittest.main()
