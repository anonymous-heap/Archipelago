"""
Tests for the Experimental options group, its preset, and the options that newer options replace.

Soul Shuffle's matched value is new, so "random" draws only the released values, and a released
YAML rolls as it always has. Enemy Drop Rate above 100 turns off Multiply Soul Drop Rates, matched
turns off Keep Soul Drop Rates, and Wide Item Smoothing sets Item Smoothing to strict in the
spheres order, so the spoiler shows what runs.

    python -m pytest worlds/cvaos/test/test_experimental_options.py -v
"""
from __future__ import annotations

import glob
import json
import os
import random
import tempfile
import unittest
import zipfile
from unittest import mock

import Generate
from Options import Choice
from test.general import setup_multiworld

from .. import CVAOSWorld
from ..options import (CVAOSOptions, EnemyChecks, EnemyDropRate, ItemDropShuffle, ItemSmoothing,
                       ItemSmoothingOrder, KeepSoulDropRates, ShuffleStartingSoul, SoulShuffle,
                       WideItemSmoothing, cvaos_option_groups, cvaos_options_presets)
from .test_smoothing_location_settings import generate


class ReleasedSoulShuffle(Choice):
    """Soul Shuffle as released, before matched existed."""
    option_off = 0
    option_within_type = 1
    option_any_type = 2
    default = option_off


def early(**options) -> CVAOSWorld:
    return setup_multiworld(CVAOSWorld, ("generate_early",), seed=1, options=options).worlds[1]


class GroupAndPresetTest(unittest.TestCase):
    def test_the_experimental_group_is_collapsed_and_holds_the_new_options(self):
        groups = {group.name: group for group in cvaos_option_groups}
        self.assertNotIn("Soul Shuffle", groups)
        experimental = groups["Experimental"]
        self.assertTrue(experimental.start_collapsed)
        self.assertEqual(experimental.options, [EnemyChecks, SoulShuffle, KeepSoulDropRates,
                                                ShuffleStartingSoul, ItemDropShuffle,
                                                EnemyDropRate, WideItemSmoothing])

    def test_the_preset_turns_on_the_experimental_options(self):
        preset = cvaos_options_presets["Experimental"]
        self.assertIs(CVAOSWorld.web.options_presets, cvaos_options_presets)
        self.assertEqual(EnemyChecks.from_any(preset["enemy_checks"]), EnemyChecks.option_all)
        self.assertEqual(SoulShuffle.from_any(preset["soul_shuffle"]), SoulShuffle.option_matched)
        self.assertTrue(ItemDropShuffle.from_any(preset["item_drop_shuffle"]))
        self.assertEqual(EnemyDropRate.from_any(preset["enemy_drop_rate"]).value, 350)

    def test_item_drop_shuffle_is_off_by_default(self):
        self.assertEqual(ItemDropShuffle.default, 0)

    def test_enemy_drop_rate_is_a_percent_from_100_to_1000(self):
        self.assertEqual((EnemyDropRate.range_start, EnemyDropRate.range_end), (100, 1000))
        self.assertEqual(EnemyDropRate.default, 100)
        self.assertEqual(EnemyDropRate.special_range_names,
                         {"vanilla": 100, "double": 200, "triple": 300, "quintuple": 500,
                          "maximum": 1000})


class RandomRollTest(unittest.TestCase):
    YAML = {"game": CVAOSWorld.game, "name": "Soma",
            CVAOSWorld.game: {"soul_shuffle": "random", "goal": "random",
                              "item_smoothing": "random"}}

    def roll(self, seed: int) -> dict:
        random.seed(seed)
        rolled = Generate.roll_settings(self.YAML)
        return {name: getattr(rolled, name).value for name in CVAOSOptions.type_hints}

    def test_a_released_yaml_rolls_as_it_did_before_matched(self):
        rolled = [self.roll(seed) for seed in range(10)]
        with mock.patch.dict(CVAOSOptions.type_hints, soul_shuffle=ReleasedSoulShuffle):
            released = [self.roll(seed) for seed in range(10)]
        self.assertEqual(rolled, released)
        self.assertGreater(len({roll["soul_shuffle"] for roll in rolled}), 1)

    def test_random_never_rolls_matched(self):
        random.seed(0)
        values = {SoulShuffle.from_text("random").value for _ in range(300)}
        self.assertEqual(values, {SoulShuffle.option_off, SoulShuffle.option_within_type,
                                  SoulShuffle.option_any_type})


class EffectiveValuesTest(unittest.TestCase):
    def test_enemy_drop_rate_above_100_turns_off_multiply_soul_drop_rates(self):
        with self.assertLogs(level="INFO") as logs:
            world = early(enemy_drop_rate=350, multiply_soul_drop_rates=True)
        self.assertFalse(world.options.multiply_soul_drop_rates)
        self.assertEqual(sum("Multiply Soul Drop Rates is off" in line for line in logs.output), 1)
        self.assertTrue(early(enemy_drop_rate=100, multiply_soul_drop_rates=True)
                        .options.multiply_soul_drop_rates)

    def test_matched_turns_off_keep_soul_drop_rates(self):
        with self.assertLogs(level="INFO") as logs:
            world = early(soul_shuffle="matched", keep_soul_drop_rates=True)
        self.assertFalse(world.options.keep_soul_drop_rates)
        self.assertEqual(sum("Keep Soul Drop Rates is off" in line for line in logs.output), 1)
        self.assertTrue(early(soul_shuffle="within_type", keep_soul_drop_rates=True)
                        .options.keep_soul_drop_rates)

    def test_wide_item_smoothing_sets_item_smoothing_to_strict_in_the_spheres_order(self):
        with self.assertLogs(level="INFO") as logs:
            world = early(wide_item_smoothing=True, item_smoothing="off",
                          item_smoothing_order="map_distance")
        self.assertEqual(world.options.item_smoothing.value, ItemSmoothing.option_strict)
        self.assertEqual(world.options.item_smoothing_order.value,
                         ItemSmoothingOrder.option_spheres)
        self.assertEqual(sum("because Wide Item Smoothing sets it" in line
                             for line in logs.output), 2)
        unchanged = early(item_smoothing="loose", item_smoothing_order="map_distance").options
        self.assertEqual(unchanged.item_smoothing.value, ItemSmoothing.option_loose)
        self.assertEqual(unchanged.item_smoothing_order.value,
                         ItemSmoothingOrder.option_map_distance)

    def test_a_seed_with_enemy_drop_rate_generates_and_patches_the_value_that_runs(self):
        multiworld = generate(5, {"enemy_drop_rate": 350, "multiply_soul_drop_rates": True})
        with tempfile.TemporaryDirectory() as folder:
            multiworld.worlds[1].generate_output(folder)
            (patch_path,) = glob.glob(os.path.join(folder, "*.apcvaos"))
            with zipfile.ZipFile(patch_path) as patch_file:
                rom_config = json.loads(patch_file.read("rom_config.json"))
        self.assertIs(rom_config["multiply_soul_drop_rates"], False)


if __name__ == "__main__":
    unittest.main()
