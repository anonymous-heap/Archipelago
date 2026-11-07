"""
Tests for Item Drop Shuffle in a seed: the item pool that the moved drops join.

Each moved drop takes the place of one cheap item: the item its own check would add, another
check's filler, or one copy of a base restorative. The pool depends only on options, so Universal
Tracker rebuilds it from the slot data (test_universal_tracker covers the round trip).

    python -m pytest worlds/cvaos/test/test_item_drop_shuffle.py -v
"""
from __future__ import annotations

import unittest
from collections import Counter

from BaseClasses import MultiWorld
from test.general import setup_multiworld

from .. import CVAOSWorld
from ..data import enemy_type_by_name, item_info_collection, pickup_info_collection
from ..drop_sources import travel_plan
from ..enemy_checks import enemy_check_types
from ..item_granting import TransferCategory, pack
from ..items import (CONSUMABLE_CAP, DROP_ITEMS, HP_RESTORATIVES, check_item_names,
                     item_name_to_id, item_table)

POOL_STEPS = ("generate_early", "create_regions", "create_items")

# The rows of the design's table of option combinations that this version can generate: each
# Enemy Checks setting, with the Graham goal where it changes what moves, and Item Drop Shuffle
# off and on.
ROWS = [{"enemy_checks": checks, "goal": goal, "item_drop_shuffle": on}
        for checks, goal in (("off", "chaos"), ("bosses", "chaos"), ("bosses", "graham"),
                             ("all", "chaos"))
        for on in (False, True)]

CONSUMABLES = frozenset(info.name for info in item_info_collection
                        if info.item_category == "consumable")


def build(seed: int = 1, **options) -> MultiWorld:
    return setup_multiworld(CVAOSWorld, POOL_STEPS, seed=seed, options=options)


def pool_items(multiworld: MultiWorld) -> list:
    return [*multiworld.itempool, *multiworld.worlds[1].get_pre_fill_items()]


def pool_names(multiworld: MultiWorld) -> Counter:
    return Counter(item.name for item in pool_items(multiworld))


class PoolTest(unittest.TestCase):
    def test_every_row_has_one_item_per_location_and_no_consumable_past_the_cap(self):
        for options in ROWS:
            multiworld = build(**options)
            counts = pool_names(multiworld)
            with self.subTest(**options):
                self.assertEqual(counts.total(), len(multiworld.get_locations(1)))
                self.assertEqual({name: count for name, count in counts.items()
                                  if name in CONSUMABLES and count > CONSUMABLE_CAP}, {})

    def test_each_moved_drop_joins_the_pool_at_a_check_or_for_a_base_restorative(self):
        for options in ROWS:
            if not options["item_drop_shuffle"]:
                continue
            multiworld = build(**options)
            world = multiworld.worlds[1]
            travel = travel_plan(world)
            for_base = Counter(drop.item for drop in travel.travelling if drop.base_item)
            pickups = pool_names(build(goal=options["goal"]))
            expected_pickups = pickups - travel.base_items_replaced + for_base
            pool = pool_names(multiworld)
            added = pool - expected_pickups
            with self.subTest(**options):
                self.assertEqual(expected_pickups + added, pool)
                self.assertEqual(added.total(), len(enemy_check_types(world.options)))
                self.assertLessEqual(Counter(travel.check_items.values()), added)
        # Without checks, the 16 moved drops replace the 16 base restoratives.
        self.assertEqual(travel_plan(build(item_drop_shuffle=True).worlds[1])
                         .base_items_replaced.total(), 16)

    def test_moved_drops_add_no_progression(self):
        for options in ROWS:
            if options["item_drop_shuffle"]:
                with self.subTest(**options):
                    progression = [Counter(item.name for item in pool_items(build(**row))
                                           if item.advancement)
                                   for row in (dict(options, item_drop_shuffle=False), options)]
                    self.assertEqual(progression[1], progression[0])

    def test_moved_drops_count_before_any_rung_is_chosen(self):
        # Flame Demon's Laevatain is too dear for its check, so the check adds the rung the pool
        # holds fewest of. A moved drop at a later check already counts.
        types = [enemy_type_by_name[name] for name in ("Flame Demon", "Bat")]
        names = check_item_names(types, [], {"Bat": HP_RESTORATIVES[0]})
        self.assertEqual(names, [HP_RESTORATIVES[1], HP_RESTORATIVES[0]])

    def test_equal_options_give_an_equal_pool(self):
        options = {"enemy_checks": "all", "item_drop_shuffle": True}
        pools = [sorted((item.name, item.classification)
                        for item in pool_items(build(seed, **options))) for seed in (1, 2)]
        self.assertEqual(pools[0], pools[1])


class ItemIdTest(unittest.TestCase):
    def test_every_drop_item_has_an_id_that_no_option_changes(self):
        # A drop that no pickup holds is an OTHER_ITEM, numbered by its item number alone.
        held_by_a_pickup = {pickup.item_name for pickup in pickup_info_collection}
        numbers = {info.name: info.item_number for info in item_info_collection}
        for name in sorted(DROP_ITEMS):
            with self.subTest(item=name):
                self.assertIn(name, item_name_to_id)
                if name not in held_by_a_pickup:
                    self.assertEqual(item_table[name].code,
                                     pack(TransferCategory.OTHER_ITEM, numbers[name]))


if __name__ == "__main__":
    unittest.main()
