"""
Tests for the options.

The technique-logic tests check that each Logic Difficulty Preset adds its tier's technique, that
a technique's own option overrides the preset, and that no preset puts a manual-only technique in
logic. They also check how the routing access-rule factory applies techniques, floor and ceiling
breakers, and transformation sources. One test checks how the options page names the Target
Platform values.

The other tests cover the Experimental options group, its preset, and the options that other
options override. Soul Shuffle's "random" draws only the values released before matched, so a
released YAML rolls as it always has. Enemy Drop Rate above 100 turns off Multiply Soul Drop
Rates, matched turns off Keep Soul Drop Rates, and Wide Item Smoothing sets Item Smoothing to
strict in the spheres order, so the spoiler shows the values that run.

Run from the Archipelago root directory:
    python -m pytest worlds/cvaos/test/test_options.py -v
"""
from __future__ import annotations

import glob
import json
import os
import random
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

import Generate
from Options import Choice

from .. import CVAOSWorld
from ..data import AbilityCombo
from ..options import (
    CVAOSOptions,
    EnemyChecks,
    EnemyDropRate,
    InboundsZipLogic,
    ItemDropShuffle,
    ItemSmoothing,
    ItemSmoothingOrder,
    KeepSoulDropRates,
    KickableEnemyLogic,
    LogicDifficultyPreset,
    OopsAllWhips,
    PixelPerfectLogic,
    PlatformClipLogic,
    QuickSaveResetLogic,
    ShuffleStartingSoul,
    SoulShuffle,
    TECHNIQUES,
    TargetPlatform,
    TechniqueLogic,
    Tier,
    WideItemSmoothing,
    cvaos_option_groups,
    cvaos_options_presets,
    resolve_allowed_techniques,
)
from ..regions import _create_access_rule_from_routing, _StaticRouting
from . import early, generate

_OPTION_CLASSES: dict[str, type[TechniqueLogic]] = {
    "kickable_enemy_logic": KickableEnemyLogic,
    "platform_clip_logic": PlatformClipLogic,
    "pixel_perfect_logic": PixelPerfectLogic,
    "quick_save_reset_logic": QuickSaveResetLogic,
    "inbounds_zip_logic": InboundsZipLogic,
}

_ALL_DIFFICULTIES = (LogicDifficultyPreset.option_canonical, LogicDifficultyPreset.option_standard,
                     LogicDifficultyPreset.option_advanced, LogicDifficultyPreset.option_expert)


def _options(difficulty: int = LogicDifficultyPreset.default, **overrides: str) -> SimpleNamespace:
    """An options stand-in: real option instances, with only the fields that the resolver and the
    access-rule factory read."""
    ns = SimpleNamespace(logic_difficulty_preset=LogicDifficultyPreset(difficulty),
                         oops_all_whips=OopsAllWhips.from_any(overrides.get("oops_all_whips", 0)))
    for attr, cls in _OPTION_CLASSES.items():
        ns.__dict__[attr] = cls.from_any(overrides.get(attr, "inherit"))
    return ns


class _State:
    """CollectionState stand-in for access rules: has_all and has_any over a fixed item set, plus
    can_reach_region over a fixed region-name set (for Tform rules)."""

    def __init__(self, *items: str, regions: tuple[str, ...] = ()) -> None:
        self.items = set(items)
        self.regions = set(regions)

    def has_all(self, names, player) -> bool:
        return set(names) <= self.items

    def has_any(self, names, player) -> bool:
        return bool(set(names) & self.items)

    def can_reach_region(self, name, player) -> bool:
        return name in self.regions


def _rule(masks: tuple[int, ...], difficulty: int = LogicDifficultyPreset.default,
          enemy_regions: dict[int, str] | None = None, **overrides: str):
    world = SimpleNamespace(player=1, options=_options(difficulty, **overrides),
                            enemy_region_name_by_number=enemy_regions or {})
    return _create_access_rule_from_routing(_StaticRouting(masks), world)


# --- resolver: precedence chain ----------------------------------------------------------

def test_tiers_accumulate_techniques():
    """Each tier adds its technique on top of the previous tier's (per the option docstrings)."""
    by_difficulty = {d: resolve_allowed_techniques(_options(d)) for d in _ALL_DIFFICULTIES}
    assert by_difficulty[LogicDifficultyPreset.option_canonical] == AbilityCombo.None_
    assert by_difficulty[LogicDifficultyPreset.option_standard] == AbilityCombo.Enemy
    assert by_difficulty[LogicDifficultyPreset.option_advanced] == AbilityCombo.Enemy | AbilityCombo.Clip
    assert by_difficulty[LogicDifficultyPreset.option_expert] == \
        AbilityCombo.Enemy | AbilityCombo.Clip | AbilityCombo.PixPer


def test_override_out_of_logic_beats_tier():
    allowed = resolve_allowed_techniques(
        _options(LogicDifficultyPreset.option_expert, pixel_perfect_logic="out_of_logic"))
    assert not allowed & AbilityCombo.PixPer
    assert allowed & AbilityCombo.Clip  # the rest of the tier is untouched


def test_override_in_logic_beats_tier_silence_and_default():
    allowed = resolve_allowed_techniques(_options(platform_clip_logic="in_logic"))
    assert allowed == AbilityCombo.Clip


def test_manual_only_techniques_ignore_every_tier():
    """A Tier.MANUAL technique (QSave or InZip) is out of logic at every difficulty unless forced
    in."""
    manual = [t for t in TECHNIQUES if t.tier is Tier.MANUAL]
    assert manual, "expected at least one manual-only technique (QSave)"
    for technique in manual:
        for difficulty in _ALL_DIFFICULTIES:
            assert not resolve_allowed_techniques(_options(difficulty)) & technique.bit
            assert resolve_allowed_techniques(
                _options(difficulty, **{technique.option: "in_logic"})) & technique.bit


# --- registry consistency ----------------------------------------------------------------

def test_registry_options_exist_on_dataclass():
    for technique in TECHNIQUES:
        assert technique.option in CVAOSOptions.__dataclass_fields__, \
            f"TECHNIQUES references missing option {technique.option!r}"


def test_registry_bits_are_unique_single_techniques():
    bits = [technique.bit for technique in TECHNIQUES]
    assert len(bits) == len(set(bits)), "duplicate technique bits in TECHNIQUES"
    for bit in bits:
        assert bit and not bit & (bit - 1), f"{bit!r} is not a single flag"
        assert bit is not AbilityCombo.Impossible, "Impossible must never be gateable"


# --- access-rule factory: technique gating -----------------------------------------------

def test_rule_prunes_out_of_logic_technique():
    """DJump+PixPer at canonical: the only way through is out of logic, so it is never
    accessible."""
    rule = _rule((int(AbilityCombo.DJump | AbilityCombo.PixPer),))
    assert rule is not None
    assert not rule(_State("Malphas"))


def test_rule_passes_in_logic_technique_on_souls():
    """Same mask with PixPer forced in: gates purely on the soul item."""
    rule = _rule((int(AbilityCombo.DJump | AbilityCombo.PixPer),),
                 pixel_perfect_logic="in_logic")
    assert rule is not None
    assert rule(_State("Malphas"))
    assert not rule(_State())


def test_rule_soulless_in_logic_way_is_always_open():
    """A QSave-only mask with QSave forced in needs nothing else, so there is no rule at all."""
    assert _rule((int(AbilityCombo.QSave),), quick_save_reset_logic="in_logic") is None


def test_rule_world_fact_bits_are_out_of_logic():
    """Vert has no requirement yet, so a way that needs it is pruned as out of logic.

    A Vert+DJump-only way is unreachable, even with Malphas. Floor and Ceil, by contrast, resolve
    to breaker items. TODO: revisit once Vert has a requirement (see regions._ROOM_TECHNIQUE_BITS).
    """
    rule = _rule((int(AbilityCombo.Vert | AbilityCombo.DJump),))
    assert rule is not None
    assert not rule(_State("Malphas"))
    assert not rule(_State())


def test_a_weapon_breaks_a_ceiling_unless_every_weapon_is_a_whip():
    rule = _rule((int(AbilityCombo.Ceil),))
    assert rule(_State("Great Sword"))
    assert not rule(_State("Whip Sword"))
    whips = _rule((int(AbilityCombo.Ceil),), oops_all_whips=1)
    assert not whips(_State("Great Sword")), "every weapon swings like the Whip Sword"
    assert whips(_State("Black Panther"))


def test_rule_impossible_mask_stays_impossible():
    rule = _rule((int(AbilityCombo.Impossible),))
    assert rule is not None
    assert not rule(_State("Malphas", "Flying Armor"))


def test_rule_falls_back_to_other_way_through():
    """(DJump+PixPer | Bat) at canonical: the trick route is pruned, the Bat route remains."""
    rule = _rule((int(AbilityCombo.DJump | AbilityCombo.PixPer), int(AbilityCombo.Bat)))
    assert rule is not None
    assert rule(_State("Giant Bat"))
    assert not rule(_State("Malphas"))


# --- access-rule factory: Tform (transformation) gating ----------------------------------

def _a_devil_number() -> int:
    """A real Devil enemy_number from the loaded routing data."""
    from ..data import by_enemy_name_for_enemy_regions
    return next(iter(by_enemy_name_for_enemy_regions["Devil"]))


def test_rule_tform_needs_a_reachable_transformation_source():
    """WWalk+Tform with no Devil/Manticore region reachable is closed, even with Undine."""
    rule = _rule((int(AbilityCombo.WWalk | AbilityCombo.Tform),))
    assert rule is not None
    assert not rule(_State("Undine"))


def test_rule_tform_gates_on_souls_once_source_is_reachable():
    devil_region = "Enemy: Devil (test)"
    rule = _rule((int(AbilityCombo.WWalk | AbilityCombo.Tform),),
                 enemy_regions={_a_devil_number(): devil_region})
    assert rule is not None
    assert rule(_State("Undine", regions=(devil_region,)))
    assert not rule(_State("Undine"))  # source region not reachable
    assert not rule(_State(regions=(devil_region,)))  # missing Undine


def test_rule_tform_only_mask_is_exactly_source_reachability():
    devil_region = "Enemy: Devil (test)"
    rule = _rule((int(AbilityCombo.Tform),), enemy_regions={_a_devil_number(): devil_region})
    assert rule is not None
    assert rule(_State(regions=(devil_region,)))
    assert not rule(_State())


def test_target_platform_values_are_named_as_players_read_them():
    # The options page and the Options Creator show these names, so GBA stays an abbreviation.
    assert TargetPlatform.get_option_name(TargetPlatform.option_gba) == "GBA"
    collection = TargetPlatform.option_advance_collection
    assert TargetPlatform.get_option_name(collection) == "Advance Collection"


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
                                                ShuffleStartingSoul, ItemDropShuffle,
                                                EnemyDropRate, WideItemSmoothing])

    def test_the_preset_turns_on_the_experimental_options(self):
        preset = cvaos_options_presets["Experimental"]
        self.assertIs(CVAOSWorld.web.options_presets, cvaos_options_presets)
        self.assertEqual(EnemyChecks.from_any(preset["enemy_checks"]), EnemyChecks.option_all)
        self.assertEqual(SoulShuffle.from_any(preset["soul_shuffle"]), SoulShuffle.option_matched)
        self.assertTrue(ItemDropShuffle.from_any(preset["item_drop_shuffle"]))
        self.assertEqual(EnemyDropRate.from_any(preset["enemy_drop_rate"]).value, 350)
        self.assertIs(preset["wide_item_smoothing"], True)

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
