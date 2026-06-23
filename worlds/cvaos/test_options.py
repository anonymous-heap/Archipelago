"""
Tests for the technique-logic options: the resolution precedence chain
(per-technique override > difficulty tier > built-in default), the QSave-is-manual-only
rule, and technique gating through the routing access-rule factory.

Run from the Archipelago root directory:
    python -m pytest worlds/cvaos/test_options.py -v
"""
from __future__ import annotations

from types import SimpleNamespace

from .data import AbilityCombo
from .options import (
    CVAOSOptions,
    KickableEnemyLogic,
    LogicDifficultyPreset,
    PixelPerfectLogic,
    PlatformClipLogic,
    QuickSaveResetLogic,
    TECHNIQUES,
    TechniqueLogic,
    Tier,
    resolve_allowed_techniques,
)
from .regions import _create_access_rule_from_routing, _StaticRouting

_OPTION_CLASSES: dict[str, type[TechniqueLogic]] = {
    "kickable_enemy_logic": KickableEnemyLogic,
    "platform_clip_logic": PlatformClipLogic,
    "pixel_perfect_logic": PixelPerfectLogic,
    "quick_save_reset_logic": QuickSaveResetLogic,
}

_ALL_DIFFICULTIES = (LogicDifficultyPreset.option_canonical, LogicDifficultyPreset.option_standard,
                     LogicDifficultyPreset.option_advanced, LogicDifficultyPreset.option_expert)


def _options(difficulty: int = LogicDifficultyPreset.default, **overrides: str) -> SimpleNamespace:
    """An options stand-in: real option instances, only the fields the resolver reads."""
    ns = SimpleNamespace(logic_difficulty_preset=LogicDifficultyPreset(difficulty))
    for attr, cls in _OPTION_CLASSES.items():
        ns.__dict__[attr] = cls.from_any(overrides.get(attr, "inherit"))
    return ns


class _State:
    """CollectionState stand-in for access rules: has_all over a fixed item set, plus
    can_reach_region over a fixed region-name set (for Tform rules)."""

    def __init__(self, *items: str, regions: tuple[str, ...] = ()) -> None:
        self.items = set(items)
        self.regions = set(regions)

    def has_all(self, names, player) -> bool:
        return set(names) <= self.items

    def can_reach_region(self, name, player) -> bool:
        return name in self.regions


def _rule(masks: tuple[int, ...], difficulty: int = LogicDifficultyPreset.default,
          enemy_regions: dict[int, str] | None = None, **overrides: str):
    world = SimpleNamespace(player=1, options=_options(difficulty, **overrides),
                            enemy_region_name_by_number=enemy_regions or {})
    return _create_access_rule_from_routing(_StaticRouting(masks), world)


# --- resolver: precedence chain ----------------------------------------------------------

def test_default_has_nothing_in_logic():
    assert resolve_allowed_techniques(_options()) == AbilityCombo.None_


def test_tiers_accumulate_techniques():
    """Each tier adds its technique on top of the previous tier's (per the option docstrings)."""
    by_difficulty = {d: resolve_allowed_techniques(_options(d)) for d in _ALL_DIFFICULTIES}
    assert by_difficulty[LogicDifficultyPreset.option_canonical] == AbilityCombo.None_
    assert by_difficulty[LogicDifficultyPreset.option_standard] == AbilityCombo.Enemy
    assert by_difficulty[LogicDifficultyPreset.option_advanced] == AbilityCombo.Enemy | AbilityCombo.Clip
    assert by_difficulty[LogicDifficultyPreset.option_expert] == \
        AbilityCombo.Enemy | AbilityCombo.Clip | AbilityCombo.PixPer
    # Monotonic: raising the difficulty never removes a technique.
    for lower, higher in zip(_ALL_DIFFICULTIES, _ALL_DIFFICULTIES[1:]):
        assert by_difficulty[lower] & by_difficulty[higher] == by_difficulty[lower]


def test_override_out_of_logic_beats_tier():
    allowed = resolve_allowed_techniques(
        _options(LogicDifficultyPreset.option_expert, pixel_perfect_logic="out_of_logic"))
    assert not allowed & AbilityCombo.PixPer
    assert allowed & AbilityCombo.Clip  # the rest of the tier is untouched


def test_override_in_logic_beats_tier_silence_and_default():
    allowed = resolve_allowed_techniques(_options(platform_clip_logic="in_logic"))
    assert allowed == AbilityCombo.Clip


def test_manual_only_techniques_ignore_every_tier():
    """A Tier.MANUAL technique (QSave) is out of logic at every difficulty unless forced in."""
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
    """DJump+PixPer at canonical: the only way through is out of logic -> never accessible."""
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
    """A QSave-only mask with QSave forced in needs nothing else -> no rule at all."""
    assert _rule((int(AbilityCombo.QSave),), quick_save_reset_logic="in_logic") is None


def test_rule_world_fact_bits_are_out_of_logic():
    """Vert has no implemented requirement yet, so an option requiring it is pruned as out of logic:
    a Vert+DJump-only way is unreachable, not satisfied by Malphas alone. (Floor/Ceil, by contrast,
    now resolve to breaker items.) TODO: revisit once Vert is coded (see regions._ROOM_TECHNIQUE_BITS)."""
    rule = _rule((int(AbilityCombo.Vert | AbilityCombo.DJump),))
    assert rule is not None
    assert not rule(_State("Malphas"))
    assert not rule(_State())


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
    from .data import by_enemy_name_for_enemy_regions
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
