"""
Tests for the drop table that the patch writes (rom/drop_table.py).

Each drop slot stores its item's number plus 1. The decode test reads a patched table back with the
spawn routine's own ranges, so an off-by-one in the writer shows up as a wrong item.

    python -m pytest worlds/cvaos/test/test_drop_table.py -v
"""
from __future__ import annotations

import itertools
import random
import unittest

from test.general import setup_multiworld

from .. import CVAOSWorld
from ..data import enemy_type_by_name, enemy_type_collection, item_info_collection
from ..data.item_info import by_category_id, by_name as item_info_by_name
from ..drop_shuffle import (HAMMER_STOCK, DropSlot, final_drop_table, plan_drop_shuffle,
                            vanilla_drop_table)
from ..options import EnemyChecks, Goal
from ..rom.drop_table import build_writes, encode_table, stored_value, vanilla_bytes
from ..rom.enemy_table import TABLE_OFFSET, TABLE_SIZE, field_offset, parse_table
from . import base_roms

# Every Enemy Checks, Goal and Wide Item Smoothing combination that the drop plan takes.
PLAN_SETTINGS = list(itertools.product(
    (EnemyChecks.option_off, EnemyChecks.option_bosses, EnemyChecks.option_all),
    (Goal.option_chaos, Goal.option_graham), (False, True)))
EQUIPMENT = ("weapon", "armor", "accessory")
ARMOR_COUNT = sum(1 for info in item_info_collection if info.item_category == "armor")


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
