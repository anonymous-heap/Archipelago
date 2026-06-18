"""
Options for the AP Castlevania: Aria of Sorrow randomizer.

Routing requirement masks (data/routing_info) may require execution
techniques (pixel-perfect platforming, platform clips, ...) on top of dev-intended
(canonical) logic.
``LogicDifficultyPreset`` picks which techniques logic may expect by default,
and each technique has an inherit/in_logic/out_of_logic option. ``resolve_allowed_techniques``
resolves them with this priority:
    1. per-technique override
    2. difficulty tier presets (the TECHNIQUES registry)
"""

from dataclasses import dataclass
from enum import IntEnum

from Options import Choice, DeathLink, DefaultOnToggle, ItemsAccessibility, PerGameCommonOptions, Toggle

from .data import AbilityCombo


class RandomizePickups(Toggle):
    """
    Adds the one-time on-the-ground pickups to the randomization pool.
    """
    display_name = "Randomize Pickups"
    default = 1


class Goal(Choice):
    """
    The win condition.

    - chaos: defeat Chaos form two.
    - graham: defeat Graham (with any equipment).
    """
    display_name = "Goal"
    option_chaos = 0
    option_graham = 1
    default = option_chaos


class HardMode(Toggle):
    """
    Turn on Hard Mode (the client forces the relevant nibble).

    Shuffle the game's Hard Mode pickups (Kaiser Knuckle, Death's Sickle, Death's Robe,
    Silver Gun, Tear of Blood). The game only spawns those five in Hard Mode, so when this is
    off they are excluded from the location pool and item pool entirely.

    Make enemies take less damage.

    Ostensibly make unearned souls easier to obtain.
    """
    display_name = "Hard Mode"
    default = 0


class SkullKeyWarp(DefaultOnToggle):
    """
    Turn the Skull Key into a warp item: using it from the Consumables menu teleports Soma to the
    starting room.
    """
    display_name = "Skull Key Warp"


class LogicDifficultyPreset(Choice):
    """
    How much execution skill the logic may expect, beyond having the right souls.
    Each tier includes the ones below it.

    - canonical: developer-intended routes only.
    - standard: may expect kickable-enemy launches.
    - advanced: may also expect platform clips.
    - expert: may also expect pixel-perfect platforming.

    Quick-save resets are never expected; see Quick-Save Reset Logic.
    Each technique can be forced in or out of logic with its own option.
    """
    display_name = "Logic Difficulty Preset"
    option_canonical = 0
    option_standard = 1
    option_advanced = 2
    option_expert = 3
    default = option_canonical


class TechniqueLogic(Choice):
    """
    Tri-state per-technique override: ``inherit`` follows the Logic Difficulty Preset;
    ``in_logic``/``out_of_logic`` force the technique.
    """
    option_inherit = 0
    option_in_logic = 1
    option_out_of_logic = 2
    default = option_inherit


class KickableEnemyLogic(TechniqueLogic):
    """
    Whether logic may require launching off a kickable enemy.
    inherit: in logic from Standard difficulty up.
    """
    display_name = "Kickable-Enemy Logic"


class PlatformClipLogic(TechniqueLogic):
    """
    Whether logic may require clipping the edge of platforms.
    inherit: in logic from Advanced difficulty up.
    """
    display_name = "Platform Clip Logic"


class PixelPerfectLogic(TechniqueLogic):
    """
    Whether logic may require pixel-perfect platforming.
    inherit: in logic at Expert difficulty.
    """
    display_name = "Pixel-Perfect Logic"


class QuickSaveResetLogic(TechniqueLogic):
    """
    Whether logic may require a quick save + reset, which respawns Soma at the room
    entrance. No difficulty expects this; it is in logic only if set to in_logic.
    """
    display_name = "Quick-Save Reset Logic"


class Tier(IntEnum):
    """
    Lowest ``LogicDifficultyPreset`` at which a technique is in logic by default.

    ``MANUAL`` sits above every real difficulty, so a ``MANUAL`` technique is never
    tier-enabled -- only its per-technique override can put it in logic.
    """
    CANONICAL = LogicDifficultyPreset.option_canonical
    STANDARD = LogicDifficultyPreset.option_standard
    ADVANCED = LogicDifficultyPreset.option_advanced
    EXPERT = LogicDifficultyPreset.option_expert
    MANUAL = 99


@dataclass(frozen=True)
class Technique:
    bit: AbilityCombo
    option: str
    tier: Tier


# The source of truth for gateable techniques. Tier assignments here must match the
# option docstrings above (the player-facing contract). One-technique-per-tier is incidental:
# tiers may share techniques.
TECHNIQUES: tuple[Technique, ...] = (
    Technique(AbilityCombo.Enemy,  "kickable_enemy_logic",   Tier.STANDARD),
    Technique(AbilityCombo.Clip,   "platform_clip_logic",    Tier.ADVANCED),
    Technique(AbilityCombo.PixPer, "pixel_perfect_logic",    Tier.EXPERT),
    Technique(AbilityCombo.QSave,  "quick_save_reset_logic", Tier.MANUAL),
)


@dataclass
class CVAOSOptions(PerGameCommonOptions):
    """Options for the Castlevania: Aria of Sorrow randomizer."""
    accessibility: ItemsAccessibility
    randomize_pickups: RandomizePickups
    goal: Goal
    hard_mode: HardMode
    skull_key_warp: SkullKeyWarp
    logic_difficulty_preset: LogicDifficultyPreset
    kickable_enemy_logic: KickableEnemyLogic
    platform_clip_logic: PlatformClipLogic
    pixel_perfect_logic: PixelPerfectLogic
    quick_save_reset_logic: QuickSaveResetLogic
    death_link: DeathLink


def resolve_allowed_techniques(options: CVAOSOptions) -> AbilityCombo:
    """Techniques in logic, as an AbilityCombo mask: per-technique override > tier > default."""
    difficulty = options.logic_difficulty_preset.value
    allowed = AbilityCombo.None_
    for technique in TECHNIQUES:
        match getattr(options, technique.option).value:
            case TechniqueLogic.option_in_logic:
                in_logic = True
            case TechniqueLogic.option_out_of_logic:
                in_logic = False
            case _:  # inherit
                in_logic = technique.tier <= difficulty
        if in_logic:
            allowed |= technique.bit
    return allowed
