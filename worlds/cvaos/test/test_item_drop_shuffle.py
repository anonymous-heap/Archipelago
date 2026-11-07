"""
Tests for Item Drop Shuffle in a seed: the item pool that the moved drops join, and the drop table
that the patch writes.

Each moved drop takes the place of one cheap item: the item its own check would add, another
check's filler, or one copy of a base restorative. The pool depends only on options, so Universal
Tracker rebuilds it from the slot data (test_universal_tracker covers the round trip).

Each drop slot stores its item's number plus 1. The decode test reads a patched table back with the
spawn routine's own ranges, so an off-by-one in the writer shows up as a wrong item.

    python -m pytest worlds/cvaos/test/test_item_drop_shuffle.py -v
"""
from __future__ import annotations

import itertools
import random
import unittest
from collections import Counter

from BaseClasses import MultiWorld
from test.general import setup_multiworld

from .. import CVAOSWorld
from ..data import (enemy_type_by_name, enemy_type_collection, item_info_collection,
                    pickup_info_collection)
from ..data.item_info import by_category_id, by_name as item_info_by_name
from ..drop_shuffle import HAMMER_STOCK, DropSlot, plan_drop_shuffle, vanilla_drop_table
from ..drop_sources import final_drop_table, travel_plan
from ..enemy_checks import enemy_check_types
from ..item_granting import TransferCategory, pack
from ..items import (CONSUMABLE_CAP, DROP_ITEMS, HP_RESTORATIVES, check_item_names,
                     item_name_to_id, item_table)
from ..options import EnemyChecks, Goal
from ..rom.drop_table import build_writes, encode_table, stored_value, vanilla_bytes
from ..rom.enemy_table import TABLE_OFFSET, TABLE_SIZE, field_offset, parse_table
from . import base_roms

POOL_STEPS = ("generate_early", "create_regions", "create_items")

# Every Enemy Checks, Goal and Wide Item Smoothing combination that the drop plan takes.
PLAN_SETTINGS = list(itertools.product(
    (EnemyChecks.option_off, EnemyChecks.option_bosses, EnemyChecks.option_all),
    (Goal.option_chaos, Goal.option_graham), (False, True)))
EQUIPMENT = ("weapon", "armor", "accessory")
ARMOR_COUNT = sum(1 for info in item_info_collection if info.item_category == "armor")

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


def stand_in() -> bytes:
    """Return a stand-in for the base ROM that holds only the enemy table's vanilla drop fields."""
    rom = bytearray(TABLE_OFFSET + TABLE_SIZE)
    for offset, data in vanilla_bytes().items():
        rom[offset:offset + len(data)] = data
    return bytes(rom)


def game_decode(value: int) -> str | None:
    """Name the item that a drop slot's stored value spawns, by the spawn routine's ranges.

    sub_08044054 subtracts 1, then reads up to 0x1F as a consumable, up to 0x5A as a weapon, and
    up to 0x87 as a record of the armor table, which holds the armors and then the accessories.
    """
    if value == 0:
        return None
    number = value - 1
    if number <= 0x1F:
        return by_category_id[("consumable", number)].name
    if number <= 0x5A:
        return by_category_id[("weapon", number - 0x20)].name
    if number <= 0x87:
        index = number - 0x5B
        if index < ARMOR_COUNT:
            return by_category_id[("armor", index)].name
        return by_category_id[("accessory", index - ARMOR_COUNT)].name
    raise AssertionError(f"the game reads an item record at address 0 for {value:#x}")


def decoded_table(rom: bytes) -> dict[int, tuple[DropSlot, DropSlot]]:
    rows = parse_table(rom[TABLE_OFFSET:TABLE_OFFSET + TABLE_SIZE])
    return {enemy_id: (DropSlot(game_decode(row.drop_1), row.drop_1_rarity),
                       DropSlot(game_decode(row.drop_2), row.drop_2_rarity))
            for enemy_id, row in enumerate(rows)}


def patched(base: bytes, table) -> bytes:
    rom = bytearray(base)
    for offset, data in build_writes(base, encode_table(table)).items():
        rom[offset:offset + len(data)] = data
    return bytes(rom)


class WriterTest(unittest.TestCase):
    def test_each_slot_stores_the_item_number_plus_1(self):
        # The live test's range boundaries, and both ends of the range.
        expected = {"Potion": 1, "Knife": 0x21, "Silver Gun": 0x59, "Casual Clothes": 0x5C,
                    "Gold Ring": 0x87, "Chaos Ring": 0x88}
        self.assertEqual({name: stored_value(name) for name in expected}, expected)
        self.assertEqual(stored_value(None), 0)
        with self.assertRaisesRegex(ValueError, "0x89"):
            stored_value("Winged Skeleton")  # the first soul, right after the last accessory

    def test_the_writer_refuses_a_value_the_game_cant_read(self):
        with self.assertRaisesRegex(ValueError, "0x89"):
            build_writes(stand_in(), {"1": [0x89, 0, 0, 0]})
        with self.assertRaisesRegex(ValueError, "not a byte"):
            build_writes(stand_in(), {"1": [1, 256, 0, 0]})
        with self.assertRaisesRegex(ValueError, "enemy 113"):
            build_writes(stand_in(), {"113": [0, 0, 0, 0]})

    def test_the_writer_refuses_a_rom_whose_drops_changed(self):
        vanilla = encode_table(vanilla_drop_table())
        self.assertEqual(build_writes(stand_in(), vanilla), {})
        rom = bytearray(stand_in())
        rom[field_offset(1, "drop_1_rarity")] += 1
        with self.assertRaisesRegex(ValueError, "ROM mismatch"):
            build_writes(bytes(rom), {"1": vanilla["1"]})

    def test_a_seed_without_item_drop_shuffle_keeps_the_games_table(self):
        world = setup_multiworld(CVAOSWorld, ("generate_early",), seed=1).worlds[1]
        self.assertIsNone(final_drop_table(world))


class DecodeTest(unittest.TestCase):
    """The patched table, read back with the game's own rule, holds what the plan dealt."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bases = base_roms() or {"stand-in": stand_in()}

    def test_every_base_holds_the_vanilla_table(self):
        for name, base in self.bases.items():
            with self.subTest(base=name):
                self.assertEqual(decoded_table(base), dict(vanilla_drop_table()))

    def test_every_plan_decodes_slot_for_slot(self):
        rng = random.Random(5)
        depths = {enemy.id: rng.random() for enemy in enemy_type_collection}
        for checks, goal, wide in PLAN_SETTINGS:
            plan = plan_drop_shuffle(checks, goal, wide, depths)
            moved = {drop.item for drop in plan.travel.travelling
                     if item_info_by_name[drop.item].item_category in EQUIPMENT
                     and drop.item not in HAMMER_STOCK}
            for name, base in self.bases.items():
                table = decoded_table(patched(base, plan.table))
                with self.subTest(checks=checks, goal=goal, wide=wide, base=name):
                    self.assertEqual(table, dict(plan.table))
                    items = [slot.item for row in table.values() for slot in row if slot.item]
                    self.assertEqual(len(items), 96)
                    self.assertEqual(moved & set(items), set())

    def test_a_table_that_only_trades_vanilla_drops_is_written_too(self):
        table = dict(vanilla_drop_table())
        zombie, minotaur = enemy_type_by_name["Zombie"].id, enemy_type_by_name["Minotaur"].id
        table[zombie], table[minotaur] = ((table[minotaur][0], table[zombie][1]),
                                          (table[zombie][0], table[minotaur][1]))
        for name, base in self.bases.items():
            with self.subTest(base=name):
                self.assertEqual(decoded_table(patched(base, table)), table)


if __name__ == "__main__":
    unittest.main()
