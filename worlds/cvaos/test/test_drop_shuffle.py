"""
Tests for Item Drop Shuffle's plan (drop_shuffle.py): which drops travel into the pool, what fills
the slots they vacate, and how the progression deals the drop table. Also tests the item pool
that the moved drops join.

Each moved drop takes the place of one cheap item: the item its own check would add, another
check's filler, or one copy of a base restorative. The pool depends only on options, so Universal
Tracker rebuilds it from the slot data (test_universal_tracker covers the round trip).

    python -m pytest worlds/cvaos/test/test_drop_shuffle.py -v
"""
from __future__ import annotations

import itertools
import random
import unittest
from collections import Counter

from BaseClasses import MultiWorld
from test.general import setup_multiworld

from .. import CVAOSWorld, drop_shuffle
from ..data import (enemy_type_by_name, enemy_type_collection, item_info_collection,
                    pickup_info_collection)
from ..data.item_balancing import worth
from ..drop_shuffle import (BASE_RESTORATIVES, EXCLUDED_FROM_SUPPLY, HAMMER_STOCK, HP_WEIGHT,
                            REPLACEMENT_WINDOW, plan_drop_shuffle, plan_travel, travel_plan)
from ..enemy_checks import enemy_check_types
from ..item_granting import TransferCategory, pack
from ..items import (CONSUMABLE_CAP, DROP_ITEMS, HP_RESTORATIVES, check_item_names,
                     item_name_to_id, item_table)
from ..options import EnemyChecks, Goal

SETTINGS = list(itertools.product(
    (EnemyChecks.option_off, EnemyChecks.option_bosses, EnemyChecks.option_all),
    (Goal.option_chaos, Goal.option_graham), (False, True)))
EQUIPMENT = ("weapon", "armor", "accessory")
POOL_STEPS = ("generate_early", "create_regions", "create_items")


def category(name: str) -> str:
    return worth.by_name[name].category


def depth_maps(count: int = 5) -> list[dict[int, float]]:
    """Seeded depth maps. The last one leaves some enemies out, as an unreached enemy is."""
    rng = random.Random(7)
    maps = [{enemy.id: rng.random() for enemy in enemy_type_collection} for _ in range(count)]
    maps[-1] = {enemy_id: depth for enemy_id, depth in maps[-1].items() if enemy_id % 5}
    return maps


def table_items(plan) -> list[str]:
    return [slot.item for row in plan.table.values() for slot in row if slot.item]


class TravelTest(unittest.TestCase):
    def test_travel_sets_match_the_design(self):
        # Weapons, armor, accessories and consumables, as the design's appendix A11 counts them.
        # Wide Item Smoothing matters only with all. The Graham goal matters only with bosses,
        # where Graham's check is one place fewer.
        def expected(checks, goal, wide):
            if checks == EnemyChecks.option_off:
                return 1, 0, 0, 15
            if checks == EnemyChecks.option_bosses:
                return 1, 2, 0, 22 if goal == Goal.option_graham else 23
            return (23, 10, 11, 41) if wide else (4, 4, 11, 38)

        for checks, goal, wide in SETTINGS:
            with self.subTest(checks=checks, goal=goal, wide=wide):
                travel = plan_travel(checks, goal, wide)
                kinds = Counter(category(drop.item) for drop in travel.travelling)
                self.assertEqual((kinds["weapon"], kinds["armor"], kinds["accessory"],
                                  kinds["consumable"]), expected(checks, goal, wide))
        off = plan_travel(EnemyChecks.option_off, Goal.option_chaos, False)
        self.assertEqual([drop.item for drop in off.travelling if category(drop.item) == "weapon"],
                         ["Mach Punch"])

    def test_every_travelling_drop_takes_one_place(self):
        for checks, goal, wide in SETTINGS:
            with self.subTest(checks=checks, goal=goal, wide=wide):
                travel = plan_travel(checks, goal, wide)
                for drop in travel.travelling:
                    self.assertEqual((drop.check is None) + (drop.base_item is None), 1)
                checks_used = [drop.check for drop in travel.travelling if drop.check]
                self.assertEqual(len(checks_used), len(set(checks_used)))
                self.assertLessEqual(travel.base_items_replaced, Counter(dict(BASE_RESTORATIVES)))
        # The design's count for the preset: 64 checks add their own enemy's drop, and 21 that
        # would add gold add another enemy's drop instead.
        preset = plan_travel(EnemyChecks.option_all, Goal.option_chaos, True)
        own = [drop for drop in preset.travelling
               if drop.check == enemy_type_collection[drop.enemy_id].name]
        others = [drop for drop in preset.travelling if drop not in own]
        self.assertEqual((len(own), len(others)), (64, 21))
        self.assertFalse([drop for drop in others if any(enemy_type_by_name[drop.check].drops)])

    def test_the_base_pool_holds_the_restoratives_drops_replace(self):
        held = Counter(pickup.item_name for pickup in pickup_info_collection
                       if not pickup.is_hard_mode_only)
        for name, count in BASE_RESTORATIVES:
            self.assertGreaterEqual(held[name], count, name)

    def test_the_item_lists_name_real_equipment(self):
        for name in HAMMER_STOCK | EXCLUDED_FROM_SUPPLY:
            self.assertIn(category(name), EQUIPMENT, name)

    def test_every_replacement_is_within_the_window(self):
        for checks, goal, wide in SETTINGS:
            for drop in plan_travel(checks, goal, wide).travelling:
                gap = abs(worth.item_worth(drop.replacement) - worth.item_worth(drop.item))
                with self.subTest(checks=checks, goal=goal, wide=wide, item=drop.item):
                    self.assertLessEqual(gap, REPLACEMENT_WINDOW + 1e-9)


class TableTest(unittest.TestCase):
    def test_moved_equipment_drops_from_no_enemy_unless_hammer_sells_it(self):
        for checks, goal, wide in SETTINGS:
            travel = plan_travel(checks, goal, wide)
            moved = {drop.item for drop in travel.travelling
                     if category(drop.item) in EQUIPMENT and drop.item not in HAMMER_STOCK}
            for depths in depth_maps(2):
                with self.subTest(checks=checks, goal=goal, wide=wide):
                    plan = plan_drop_shuffle(checks, goal, wide, depths)
                    self.assertEqual(moved & set(table_items(plan)), set())
                    self.assertEqual(moved & set(travel.supply), set())

    def test_excluded_items_never_drop(self):
        for checks, goal, wide in SETTINGS:
            for depths in depth_maps(2):
                with self.subTest(checks=checks, goal=goal, wide=wide):
                    plan = plan_drop_shuffle(checks, goal, wide, depths)
                    self.assertEqual(EXCLUDED_FROM_SUPPLY & set(table_items(plan)), set())
                    self.assertEqual(EXCLUDED_FROM_SUPPLY & set(plan.travel.supply), set())

    def test_the_deal_moves_each_item_with_its_rarity_byte(self):
        # A replacement takes the vacated slot's vanilla byte, and the deal only reorders
        # (item, byte) pairs among the filled slots.
        for checks, goal, wide in SETTINGS:
            travel = plan_travel(checks, goal, wide)
            for drop in travel.travelling:
                content = travel.contents[(drop.enemy_id, drop.slot)]
                vanilla_byte = enemy_type_collection[drop.enemy_id].drop_rarities[drop.slot - 1]
                self.assertEqual((content.item, content.rarity), (drop.replacement, vanilla_byte))
            for depths in depth_maps(2):
                plan = plan_drop_shuffle(checks, goal, wide, depths)
                filled = {(enemy_id, slot): content for enemy_id, row in plan.table.items()
                          for slot, content in enumerate(row, start=1) if content.item}
                self.assertEqual(set(filled), set(travel.contents))
                self.assertEqual(Counter(filled.values()), Counter(travel.contents.values()))

    def test_dealt_worth_never_falls_as_an_enemys_level_rises(self):
        hps = [enemy.hp for enemy in enemy_type_collection]
        hp_rank = {enemy.id: sum(1 for hp in hps if hp < enemy.hp) / len(hps)
                   for enemy in enemy_type_collection}
        for checks, goal, wide in SETTINGS:
            travel = plan_travel(checks, goal, wide)
            for depths in depth_maps():
                plan = plan_drop_shuffle(checks, goal, wide, depths)
                groups: dict[tuple[int, bool], list[tuple[float, float]]] = {}
                for enemy_id, slot in travel.contents:
                    vanilla = enemy_type_collection[enemy_id].drops[slot - 1]
                    level = depths.get(enemy_id, 1.0) + HP_WEIGHT * hp_rank[enemy_id]
                    item = plan.table[enemy_id][slot - 1].item
                    groups.setdefault((slot, category(vanilla) in EQUIPMENT), []).append(
                        (level, worth.item_worth(item)))
                for group, points in groups.items():
                    with self.subTest(checks=checks, goal=goal, wide=wide, group=group):
                        worths = [value for _level, value in sorted(points)]
                        self.assertEqual(worths, sorted(worths))

    def test_equal_inputs_give_an_equal_plan(self):
        depths = depth_maps(1)[0]
        for checks, goal, wide in SETTINGS:
            with self.subTest(checks=checks, goal=goal, wide=wide):
                fresh = drop_shuffle.plan_travel.__wrapped__
                self.assertEqual(fresh(checks, goal, wide), fresh(checks, goal, wide))
                self.assertEqual(plan_drop_shuffle(checks, goal, wide, dict(depths)),
                                 plan_drop_shuffle(checks, goal, wide, dict(depths)))


# The rows of the design's table of option combinations: each Enemy Checks setting, with the Graham
# goal where it changes what moves, and Item Drop Shuffle off and on. Wide Item Smoothing changes
# what moves only with every check, so its rows use that setting.
ROWS = [{"enemy_checks": checks, "goal": goal, "item_drop_shuffle": on}
        for checks, goal in (("off", "chaos"), ("bosses", "chaos"), ("bosses", "graham"),
                             ("all", "chaos"))
        for on in (False, True)]
ROWS += [{"enemy_checks": "all", "goal": "chaos", "item_drop_shuffle": on,
          "wide_item_smoothing": True} for on in (False, True)]

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
