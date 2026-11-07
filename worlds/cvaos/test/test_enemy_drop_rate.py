"""
Tests for Enemy Drop Rate (enemy_drop_rate.py and rom/enemy_drop_rates.py).

Each kind of enemy that the seed reaches before seed depth 0.25 gets 100 plus a sixth of the boost,
and every other kind gets the full value. A strong soul on an enemy reached before 0.35 keeps its
chance, as does a common item worth 0.45 or more, and the rare slot never scales. The patch scales
the bytes in its last step, after every other feature has set them. A soul rate of 0 stays 0, and
no other rate falls below 1.

The patched-ROM tests generate the Experimental preset and apply its patch to each base ROM that
is available: a cart dump, and the Advance Collection's copy.

    python -m pytest worlds/cvaos/test/test_enemy_drop_rate.py -v
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from .. import enemy_drop_rate
from .._bytemaker_compat import sizeof
from ..data import enemy_type_by_name, enemy_type_collection
from ..data.item_balancing.soul_tiers import soul_rating
from ..data.item_balancing.worth import item_worth
from ..data.item_info import by_item_number
from ..rom import enemy_drop_rates, identifiers
from ..rom.address_space import gba_space
from ..rom.enemy_table import ENEMY_COUNT, ENEMY_TABLE, EnemyDNA, field_offset, row_offset
from ..rom.soul_drop_rates import scaled_rate
from ..seed_depth import enemy_seed_depths
from ..soul_sources import soul_name, vanilla_soul_name
from . import SCRIPTED_BOSSES, preset_seed

TABLE_END = row_offset(ENEMY_COUNT - 1) + sizeof(EnemyDNA)


def percents(value: int, depths: dict[int, float], plan: dict[int, int] | None = None,
             common_drops: dict[int, str | None] | None = None) -> dict:
    """Return drop_rate_percents for a world with Enemy Drop Rate ``value`` and these depths."""
    world = SimpleNamespace(options=SimpleNamespace(enemy_drop_rate=SimpleNamespace(value=value)))
    with mock.patch.object(enemy_drop_rate, "enemy_seed_depths", return_value=depths):
        return enemy_drop_rate.drop_rate_percents(world, plan or {}, common_drops)


def at_depth(depth: float) -> dict[int, float]:
    return {enemy.id: depth for enemy in enemy_type_collection}


def row_percents(rows: dict, enemy_id: int) -> list[int]:
    """Return one row's two percents. A row left out of ``rows`` keeps both chances."""
    return rows.get(str(enemy_id), [100, 100])


def is_strong(enemy_id: int) -> bool:
    soul = vanilla_soul_name(enemy_id)
    return soul is not None and soul_rating(soul) >= 0.6


class RuleTest(unittest.TestCase):
    def test_kinds_reached_before_a_quarter_get_a_sixth_of_the_boost(self):
        self.assertEqual(enemy_drop_rate.kind_percent(350, 0.24), 142)
        self.assertEqual(enemy_drop_rate.kind_percent(350, 0.25), 350)
        self.assertEqual(enemy_drop_rate.kind_percent(1000, 0.0), 250)

    def test_a_strong_soul_keeps_its_chance_on_an_enemy_reached_before_0_35(self):
        early, late = percents(350, at_depth(0.3)), percents(350, at_depth(0.35))
        for enemy_id in range(ENEMY_COUNT):
            if vanilla_soul_name(enemy_id) is None:
                continue
            with self.subTest(enemy=enemy_id):
                self.assertEqual(row_percents(early, enemy_id)[0],
                                 100 if is_strong(enemy_id) else 350)
                self.assertEqual(row_percents(late, enemy_id)[0], 350)

    def test_the_soul_exemption_follows_the_soul_shuffle(self):
        strong = next(e for e in range(ENEMY_COUNT) if is_strong(e))
        weak = next(e for e in range(ENEMY_COUNT) if vanilla_soul_name(e) and not is_strong(e))
        swapped = percents(350, at_depth(0.3), {strong: weak, weak: strong})
        self.assertEqual(row_percents(swapped, weak)[0], 100)
        self.assertEqual(row_percents(swapped, strong)[0], 350)

    def test_only_common_items_worth_under_0_45_scale(self):
        rows = percents(350, at_depth(0.5))
        for enemy in enemy_type_collection:
            common = enemy.drops[0]
            expected = 350 if common and item_worth(common) < 0.45 else 100
            with self.subTest(enemy=enemy.name, common=common):
                self.assertEqual(row_percents(rows, enemy.id)[1], expected)
        minotaur = enemy_type_by_name["Minotaur"].id
        self.assertEqual(row_percents(rows, minotaur)[1], 100)  # its Battle Axe is worth 0.80

    def test_the_item_exemption_reads_the_dealt_common_drop(self):
        zombie, minotaur = enemy_type_by_name["Zombie"].id, enemy_type_by_name["Minotaur"].id
        dealt = {zombie: "Battle Axe", minotaur: "Potion"}
        rows = percents(350, at_depth(0.5), common_drops=dealt)
        self.assertEqual(row_percents(rows, zombie)[1], 100)
        self.assertEqual(row_percents(rows, minotaur)[1], 350)
        # An enemy missing from the dealt table has an empty common slot.
        self.assertEqual(row_percents(rows, enemy_type_by_name["Skeleton Knight"].id)[1], 100)

    def test_an_enemy_without_a_seed_depth_counts_as_depth_one(self):
        self.assertEqual(percents(350, {}), percents(350, at_depth(1.0)))

    def test_at_100_nothing_changes(self):
        self.assertEqual(percents(100, at_depth(0.0)), {})


class BuildWritesTest(unittest.TestCase):
    def test_only_the_listed_rows_soul_rate_and_common_rarity_scale(self):
        rom = bytearray(TABLE_END)
        for enemy_id in range(ENEMY_COUNT):
            for name in ("soul_rate", "drop_1_rarity", "drop_2_rarity"):
                rom[field_offset(enemy_id, name)] = (0, 1, 7, 20, 180)[enemy_id % 5]
        listed = {str(e): [350, 142] for e in range(0, ENEMY_COUNT, 2)}
        patched = bytearray(rom)
        for offset, data in enemy_drop_rates.build_writes(bytes(rom), listed).items():
            patched[offset:offset + len(data)] = data
        for enemy_id in range(ENEMY_COUNT):
            old = (0, 1, 7, 20, 180)[enemy_id % 5]
            soul, common = listed.get(str(enemy_id), [100, 100])
            with self.subTest(enemy=enemy_id):
                self.assertEqual(patched[field_offset(enemy_id, "soul_rate")],
                                 scaled_rate(old, soul))
                self.assertEqual(patched[field_offset(enemy_id, "drop_1_rarity")],
                                 scaled_rate(old, common))
                self.assertEqual(patched[field_offset(enemy_id, "drop_2_rarity")], old)
        with self.assertRaisesRegex(ValueError, "enemy 113"):
            enemy_drop_rates.build_writes(bytes(rom), {"113": [350, 350]})


def rows_of(rom: bytes) -> list:
    return ENEMY_TABLE.bind(gba_space(rom)).read()


class PresetPatchTest(unittest.TestCase):
    """The Experimental preset generates, and its patch applies to each available base ROM."""

    @classmethod
    def setUpClass(cls) -> None:
        seed = preset_seed()
        cls.depths = enemy_seed_depths(seed.world)
        cls.config, cls.patched = seed.config, seed.patched

    def patched_roms(self) -> dict[str, tuple[bytes, bytes]]:
        """Return each base's ROM before the patch's last step and after it, or skip the test when
        no base ROM is available."""
        if not self.patched:
            self.skipTest("needs a USA Aria of Sorrow ROM")
        return self.patched

    def kind(self, enemy_id: int) -> int:
        return 142 if self.depths[enemy_id] < 0.25 else 350

    def test_the_preset_generates_and_patches_in_order(self):
        self.assertTrue(self.config["soul_shuffle_plan"])
        self.assertIs(self.config["keep_soul_drop_rates"], False)
        self.assertTrue(self.config["enemy_drop_rates"])
        self.assertTrue(self.config["drop_table"])
        start = identifiers.ARCHIPELAGO_IDENTIFIER_START
        for name, (before, after) in self.patched_roms().items():
            with self.subTest(base=name):
                self.assertEqual(after[start:start + 13], b"CVAOS_AP_V0.6")
                # The overrides for Manticore, Devil and Curly applied before the last step.
                rows = rows_of(before)
                self.assertEqual([rows[e].soul_rate for e in (54, 48, 43)], [5, 5, 9])

    def test_every_soul_that_rolled_still_rolls_and_scripted_bosses_stay_at_zero(self):
        for name, (before, after) in self.patched_roms().items():
            for enemy_id, (old, new) in enumerate(zip(rows_of(before), rows_of(after))):
                with self.subTest(base=name, enemy=enemy_id):
                    if old.soul_index and old.soul_rate:
                        self.assertGreater(new.soul_rate, 0)
                    if enemy_id in SCRIPTED_BOSSES:
                        self.assertEqual((old.soul_rate, new.soul_rate), (0, 0))

    def test_soul_rates_follow_the_step_except_strong_souls_reached_before_0_35(self):
        steps, exempt = set(), 0
        for name, (before, after) in self.patched_roms().items():
            for enemy_id, (old, new) in enumerate(zip(rows_of(before), rows_of(after))):
                if not old.soul_index:
                    continue
                soul = soul_name(old.soul_type, old.soul_index)
                if soul_rating(soul) >= 0.6 and self.depths[enemy_id] < 0.35:
                    expected, exempt = old.soul_rate, exempt + 1
                else:
                    expected = scaled_rate(old.soul_rate, self.kind(enemy_id))
                    steps.add(self.kind(enemy_id))
                with self.subTest(base=name, enemy=enemy_id, soul=soul):
                    self.assertEqual(new.soul_rate, expected)
        self.assertEqual(steps, {142, 350})
        self.assertGreater(exempt, 0)

    def test_each_item_slot_follows_its_rule(self):
        kept = 0
        for name, (before, after) in self.patched_roms().items():
            for enemy, old, new in zip(enemy_type_collection, rows_of(before), rows_of(after)):
                if old.drop_1 and item_worth(by_item_number[old.drop_1 - 1].name) < 0.45:
                    expected = scaled_rate(old.drop_1_rarity, self.kind(enemy.id))
                else:
                    expected, kept = old.drop_1_rarity, kept + bool(old.drop_1)
                with self.subTest(base=name, enemy=enemy.name):
                    # Before the last step, the row holds the drop table that the config lists.
                    self.assertEqual([old.drop_1, old.drop_1_rarity, old.drop_2, old.drop_2_rarity],
                                     self.config["drop_table"][str(enemy.id)])
                    self.assertEqual(new.drop_1_rarity, expected)
                    self.assertEqual((new.drop_1, new.drop_2, new.drop_2_rarity),
                                     (old.drop_1, old.drop_2, old.drop_2_rarity))
            # Nothing outside the table changes in the last step.
            start, end = row_offset(0), TABLE_END
            self.assertEqual((before[:start], before[end:]), (after[:start], after[end:]), name)
        self.assertGreater(kept, 0)


if __name__ == "__main__":
    unittest.main()
