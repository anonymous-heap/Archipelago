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

from Options import (Choice, DeathLink, DefaultOnToggle, ItemsAccessibility, NamedRange,
                     OptionGroup, PerGameCommonOptions, Toggle, Visibility)

from .data import AbilityCombo


class RandomizePickups(Toggle):
    """
    Not implemented yet. The one-time pickups on the ground are always shuffled, so turning this
       off changes nothing. The option stays hidden from the options page and the templates until
       it does something.
    """
    display_name = "Randomize Pickups"
    default = 1
    visibility = Visibility.none


class TargetPlatform(Choice):
    """
    Where you intend to play this seed. The generated patch is identical either way; this
    only decides, at patch-apply time, where the base ROM comes from, and lets the client warn
    if you connect the wrong one.

    - gba: play the patched ROM in an emulator (BizHawk client). Applying the patch prompts for
      the base ROM; you may point it at a GBA ROM *or* at the Advance Collection's game.exe
      (the ROM is then extracted from the collection for you).
    - advance_collection: play inside the Steam Castlevania Advance Collection. The base ROM is
      taken from the installed collection automatically (see the collection_exe host setting);
      run the "CVAoS Collection Client".
    """
    display_name = "Target Platform"
    option_gba = 0
    option_advance_collection = 1
    default = option_gba


# Slot-data / rom_config values for TargetPlatform (kept in sync with the option above so the
# clients and the patch-apply base-ROM selector can compare without importing the Choice).
TARGET_GBA = TargetPlatform.option_gba
TARGET_ADVANCE_COLLECTION = TargetPlatform.option_advance_collection


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
    Turns on Hard Mode (the client forces the relevant nibble).

    Also shuffles the game's five Hard Mode pickups while Hard Mode Pickups is left at
       with_hard_mode, its default. That option can instead shuffle them without Hard Mode, or
       leave them out of a Hard Mode seed.

    Makes enemies take less damage.

    Ostensibly makes unearned souls easier to obtain.
    """
    display_name = "Hard Mode"
    default = 0


class HardModePickups(Choice):
    """
    Whether to shuffle the five pickups that the game spawns only in Hard Mode. They are
       Kaiser Knuckle, Death's Sickle, Death's Robe, Silver Gun and Tear of Blood.

    - with_hard_mode: they are shuffled when Hard Mode is on and left out when it is off.
    - always: they are shuffled whether or not Hard Mode is on. The patch makes them spawn in
      normal mode too.
    - never: they are not shuffled. In Hard Mode they still appear and hold their vanilla items.
    """
    display_name = "Hard Mode Pickups"
    option_with_hard_mode = 0
    option_always = 1
    option_never = 2
    default = option_with_hard_mode


class SoulShuffle(Choice):
    """
    Shuffles which enemy drops which soul.

    The 7 enemies whose soul is progression (Chronomage, Curly, Devil, Manticore, Kicker
       Skeleton, Succubus, Flame Demon) always keep their own soul, so logic is unaffected and
       this is purely a discovery/variety option. The other 103 are permuted, so every soul
       still has exactly one enemy that drops it.

    - off: enemies drop their vanilla souls.
    - within_type: red souls swap with red, blue with blue, yellow with yellow (pools of 53,
      18 and 32).
    - any_type: one permutation across all 103 souls, ignoring type.
    """
    display_name = "Soul Shuffle"
    option_off = 0
    option_within_type = 1
    option_any_type = 2
    default = option_off


class KeepSoulDropRates(DefaultOnToggle):
    """
    No effect when Soul Shuffle is off.

    Whether a soul keeps its own drop rate when it moves to a new enemy, or inherits the rate
       of the enemy now dropping it.

    - on (keep soul drop rates): each soul stays exactly as rare as it is in vanilla and only
      its source changes, so the shuffle doesn't alter how much grinding the game asks for.
    - off (keep enemy drop rates): rarity becomes a property of the encounter -- a soul is as
      common as whatever now drops it. Rare enemies become worth hunting for their own sake.

    Either way, the one-time bosses Headhunter, Death, Legion and Balore still award exactly one
       soul when they die: whichever soul the shuffle gave them. In vanilla, those four souls come
       only from that scripted award, never from a random drop, so they have no rarity to keep.
       When one of those souls moves to an ordinary enemy, it drops at that enemy's usual rate.
    """
    display_name = "Keep Soul Drop Rates"


class ShuffleStartingSoul(DefaultOnToggle):
    """
    No effect when Soul Shuffle is off.

    The intro hands Soma a free Winged Skeleton soul. That is hardcoded in the intro,
       not an enemy drop, so Soul Shuffle does not touch it on its own. This
       option makes the starting soul follow the shuffle.

    Note that the cutscene will still _say_ "Winged Skeleton" in all cases for now.

    - on: the soul becomes whatever soul now drops from the Winged Skeleton enemy.
    - off: you always start with the vanilla Winged Skeleton soul.
    """
    display_name = "Shuffle Starting Soul"


class MultiplySoulDropRates(Toggle):
    """
    Makes souls drop more often across the board, independently of Soul Shuffle.

    Set the size of the increase with Soul Drop Rate Multiplier.
    """
    display_name = "Multiply Soul Drop Rates"
    default = 0


class SoulDropRateMultiplier(NamedRange):
    """
    No effect when Multiply Soul Drop Rates is off.

    How much more often souls drop, in percent: 100 is vanilla, 120 means a soul appears 1.2x
       as often, 400 means four times as often.

    There is a hard limit to how common souls can be currently, so higher multipliers will
       mostly affect rarer drops.
    """
    display_name = "Soul Drop Rate Multiplier"
    range_start = 100
    range_end = 1000
    default = 120
    special_range_names = {
        "vanilla": 100,
        "slight": 120,
        "half_again": 150,
        "double": 200,
        "quadruple": 400,
        "tenfold": 1000,
    }


class AncientBookSoulDrops(DefaultOnToggle):
    """
    While you carry Ancient Book 1, every Flame Demon you defeat drops its soul. Ancient Book 3
       does the same for the Succubus. Both books' pause-menu descriptions say so.

    Ancient Book 2 describes the Giant Bat soul. That soul is an item in the multiworld pool rather
       than an enemy drop, so this option does nothing for Book 2.

    The guaranteed drops are built into the ROM, so they work in any emulator and need no client.
    """
    display_name = "Ancient Book Soul Drops"


class SkullKeyWarp(DefaultOnToggle):
    """
    Turns the Skull Key into a warp item: using it from the Consumables menu teleports Soma to the
       starting room.
    """
    display_name = "Skull Key Warp"


class NewSoulPause(DefaultOnToggle):
    """
    When a soul you do not already own arrives from another world, show the game's first-soul box
       and stop the action until you press A, exactly as absorbing it yourself would. Off: every
       received soul gets the short banner a duplicate gets, and play is never interrupted.
    """
    display_name = "New Soul Pause"


class ForbiddenAreaButton(Choice):
    """
    How the Forbidden Area (gate at the bottom of the Study) is unlocked.

    Normally the Forbidden Area can't be entered _first_ through the Study, because a metal
        barrier blocks the way until a button on the far side is pressed.

    With pickup, the default, the button is replaced by a shuffled item.

    - off: the button is present in A01 and opens the barrier when pressed.
    - pickup: the button is removed and the unlock becomes a shuffled "Study Sealswitch" pickup.
    """
    display_name = "Forbidden Area Button"
    option_off = 0
    option_pickup = 1
    default = option_pickup


class SingleJumpDivekick(DefaultOnToggle):
    """
    Allows the divekick out of a first jump, before Malphas (Double Jump) is owned.
       Still disallowed in water without Skula.
       (From Xanthus's public AoS patch collection.)
    """
    display_name = "Single-Jump Divekick"


class ClassicvaniaMovement(Toggle):
    """
    NES-style movement with no air control: jump trajectory is committed on takeoff, and walking
       off a ledge drops nearly straight down. Direction can still be changed with Malphas (Double
       Jump) or Hippogryph, and the flight souls (Flying Armor, Giant Bat) keep normal air control.
       (From Xanthus's public AoS patch collection.)
    """
    display_name = "Classicvania Movement"
    default = 0


class OopsAllWhips(Toggle):
    """
    Every weapon attacks with the Whip Sword's swing animation.
       (From Xanthus's public AoS patch collection.)

    The whip swing does not break floors or ceilings, so while this is on, logic expects Black
        Panther for those rather than a weapon.

    Weapon stats are untouched, but effective reach follows the whip swing.
    """
    display_name = "Oops! All Whips"
    default = 0


class ItemSmoothing(Choice):
    """
    **This is the flag to turn off if you want a totally-random-yet-finishable run.**

    Biases item placement so less-desirable items show up earlier and more-desirable ones later,
       using community tier-list data (data/item_balancing).

    Most progression items are placed as usual, with three exceptions. The three Ancient Books are
       spread across the early, middle and late parts of the run. The mobility souls and Black
       Panther follow Early Mobility Souls and Black Panther Bias. Weapons whose only progression
       use is breaking floors or ceilings are ordered like other equipment.

    - off: vanilla placement -- any item can appear anywhere.
    - loose: gentle escalation with wide, long-tailed luck (more surprises, lucky early finds).
    - strict: stronger escalation with tighter placement (fewer surprises).
    """
    display_name = "Item Smoothing"
    option_off = 0
    option_loose = 1
    option_strict = 2
    default = option_strict


class ItemSmoothingOrder(Choice):
    """
    No effect when Item Smoothing is off.

    Explanation:

    When Item Smoothing is on:
        option_spheres = good stuff generally appears later in the run
        option_map_distance = good stuff generally appears in later original-game spots

    For example: does Claimh Solais (a very strong weapon) show up later in the run (spheres),
        or in a spot that would be accessed late in the original game (map_distance)?


    We default to "option_map_distance" to make sure you can find good stuff if forced into a 
        hard area.

    The Ancient Books, the mobility souls and Black Panther are placed by their own rules rather
        than this order. Weapons whose only progression use is breaking floors or ceilings do
        follow it.

    Options:

    - spheres: logical reachability order, measured after randomization -- how deep into *this
      seed's* progression a location actually sits. Seed-specific.
    - map_distance: structural distance from the start in the unrandomized map (the region graph),
      ignoring access rules and randomization. Seed-independent: a room is always "as early"
      regardless of where progression landed.
    """
    display_name = "Item Smoothing Order"
    option_spheres = 0
    option_map_distance = 1
    default = option_map_distance


class ProgressiveMovement(Toggle):
    """
    Replaces the Flying Armor, Malphas and Giant Bat souls with three Progressive Movement items.
       The first one you get gives Flying Armor, the second Malphas (double jump), and the third
       Giant Bat, whether you find them yourself or receive them from another world.

    A Progressive Movement item in your own world looks like a Skull Key, the same pickup used for
       other players' items. Picking one up gives no soul on its own. The client grants the soul,
       so it arrives only once the client is connected.
    """
    display_name = "Progressive Movement"
    default = 0


class EarlyMobilitySouls(Choice):
    """
    No effect when Item Smoothing is off.

    When Item Smoothing is on, how the three vertical mobility souls (Malphas/DJump, Hippogryph, and
       Giant Bat) are placed. They gate movement, so this is a separate axis from item desirability.
    
    - off: no special handling. The souls are placed like any other progression item.
    - bias_early: in about 93% of seeds, pull Malphas or, less often, Hippogryph into the first
      third of the run. In the other seeds, all three stay late.
    - guarantee_early: always pull Malphas or Hippogryph early, so you never start fully
      mobility-starved.

    With bias_early or guarantee_early, Giant Bat is never the soul pulled early, and it usually
       lands in the later part of the run. Its flight reaches nearly everything the other two souls
       do, so an early Giant Bat would open most of the castle at once.

    With Progressive Movement on, two of the three Progressive Movement items are placed the way
       Malphas would be, and the third the way Giant Bat would be. When Malphas is the soul pulled
       early, those two items are both aimed early, so you get Flying Armor and double jump early.
    """
    display_name = "Early Mobility Souls"
    option_off = 0
    option_bias_early = 1
    option_guarantee_early = 2
    default = option_bias_early


class BlackPantherBias(DefaultOnToggle):
    """
    No effect when Item Smoothing is off.

    When Item Smoothing is on:
       On: aim Black Panther (dash) at roughly the first half of the run (by sphere ordering).
          Logic sometimes leaves no early spot for it, so it lands there in most seeds rather
          than all of them.
       Off: Black Panther places like any other progression item.
    """
    display_name = "Black Panther Bias"


class LogicDifficultyPreset(Choice):
    """
    How much execution skill the logic may expect, beyond having the right souls.
    Each tier includes the ones below it.

    - canonical: developer-intended routes only.
    - standard: may expect kickable-enemy launches.
    - advanced: may also expect platform clips.
    - expert: may also expect pixel-perfect platforming.

    Quick-save resets and inbounds zips are never expected; see Quick-Save Reset Logic and
    Inbounds Zip Logic.
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


class InboundsZipLogic(TechniqueLogic):
    """
    Whether logic may require an inbounds zip. No difficulty expects this; it is in logic only if
    set to in_logic.
    """
    display_name = "Inbounds Zip Logic"


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
    Technique(AbilityCombo.InZip,  "inbounds_zip_logic",     Tier.MANUAL),
)


@dataclass
class CVAOSOptions(PerGameCommonOptions):
    """Options for the Castlevania: Aria of Sorrow randomizer."""
    accessibility: ItemsAccessibility
    target_platform: TargetPlatform
    randomize_pickups: RandomizePickups
    goal: Goal
    hard_mode: HardMode
    hard_mode_pickups: HardModePickups
    progressive_movement: ProgressiveMovement
    soul_shuffle: SoulShuffle
    keep_soul_drop_rates: KeepSoulDropRates
    shuffle_starting_soul: ShuffleStartingSoul
    multiply_soul_drop_rates: MultiplySoulDropRates
    soul_drop_rate_multiplier: SoulDropRateMultiplier
    ancient_book_soul_drops: AncientBookSoulDrops
    skull_key_warp: SkullKeyWarp
    new_soul_pause: NewSoulPause
    forbidden_area_button: ForbiddenAreaButton
    single_jump_divekick: SingleJumpDivekick
    classicvania_movement: ClassicvaniaMovement
    oops_all_whips: OopsAllWhips
    item_smoothing: ItemSmoothing
    item_smoothing_order: ItemSmoothingOrder
    early_mobility_souls: EarlyMobilitySouls
    black_panther_bias: BlackPantherBias
    logic_difficulty_preset: LogicDifficultyPreset
    kickable_enemy_logic: KickableEnemyLogic
    platform_clip_logic: PlatformClipLogic
    pixel_perfect_logic: PixelPerfectLogic
    quick_save_reset_logic: QuickSaveResetLogic
    inbounds_zip_logic: InboundsZipLogic
    death_link: DeathLink


# Names of the options that determine which locations exist, which items are in the pool, or what
# an access rule requires. fill_slot_data writes them into the slot data so Universal Tracker can
# rebuild the world with the logic the seed was generated with. An option that starts affecting
# logic must be added here; test_universal_tracker fails until it is.
LOGIC_OPTION_NAMES: tuple[str, ...] = (
    "goal",
    "hard_mode",
    "hard_mode_pickups",
    "progressive_movement",
    "forbidden_area_button",
    "oops_all_whips",
    "logic_difficulty_preset",
    *(technique.option for technique in TECHNIQUES),
)


cvaos_option_groups = [
    OptionGroup("Soul Shuffle", [
        SoulShuffle,
        KeepSoulDropRates,
        ShuffleStartingSoul,
    ]),
    OptionGroup("Soul Drop Rates", [
        MultiplySoulDropRates,
        SoulDropRateMultiplier,
        AncientBookSoulDrops,
    ]),
    OptionGroup("Item Smoothing", [
        ItemSmoothing,
        ItemSmoothingOrder,
        EarlyMobilitySouls,
        BlackPantherBias,
    ]),
    OptionGroup("Logic Difficulty", [
        LogicDifficultyPreset,
        KickableEnemyLogic,
        PlatformClipLogic,
        PixelPerfectLogic,
        QuickSaveResetLogic,
        InboundsZipLogic,
    ]),
    OptionGroup("Gameplay Tweaks", [
        SingleJumpDivekick,
        ClassicvaniaMovement,
        OopsAllWhips,
        NewSoulPause,
    ]),
]


def hard_mode_pickups_included(options: CVAOSOptions) -> bool:
    """Whether the five Hard Mode pickups are locations and their items are in the pool."""
    match options.hard_mode_pickups.value:
        case HardModePickups.option_always:
            return True
        case HardModePickups.option_never:
            return False
        case _:  # with_hard_mode
            return bool(options.hard_mode)


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
