"""
Tests for the Experimental options group, its preset, and Soul Shuffle's matched value.

Soul Shuffle's matched value is new, so "random" draws only the released values, and a released
YAML rolls as it always has.

    python -m pytest worlds/cvaos/test/test_experimental_options.py -v
"""
from __future__ import annotations

import random
import unittest
from unittest import mock

import Generate
from Options import Choice

from .. import CVAOSWorld
from ..options import (CVAOSOptions, EnemyChecks, EnemyDropRate, KeepSoulDropRates,
                       ShuffleStartingSoul, SoulShuffle, cvaos_option_groups,
                       cvaos_options_presets)


class ReleasedSoulShuffle(Choice):
    """Soul Shuffle as released, before matched existed."""
    option_off = 0
    option_within_type = 1
    option_any_type = 2
    default = option_off


class GroupAndPresetTest(unittest.TestCase):
    def test_the_experimental_group_is_collapsed_and_holds_the_new_options(self):
        groups = {group.name: group for group in cvaos_option_groups}
        self.assertNotIn("Soul Shuffle", groups)
        experimental = groups["Experimental"]
        self.assertTrue(experimental.start_collapsed)
        self.assertEqual(experimental.options, [EnemyChecks, SoulShuffle, KeepSoulDropRates,
                                                ShuffleStartingSoul, EnemyDropRate])

    def test_the_preset_turns_on_the_experimental_options(self):
        preset = cvaos_options_presets["Experimental"]
        self.assertIs(CVAOSWorld.web.options_presets, cvaos_options_presets)
        self.assertEqual(EnemyChecks.from_any(preset["enemy_checks"]), EnemyChecks.option_all)
        self.assertEqual(SoulShuffle.from_any(preset["soul_shuffle"]), SoulShuffle.option_matched)
        self.assertEqual(EnemyDropRate.from_any(preset["enemy_drop_rate"]).value, 350)

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


if __name__ == "__main__":
    unittest.main()
