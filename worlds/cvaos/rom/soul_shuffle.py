"""
Shuffle which soul each enemy drops.

The enemy table is a flat array at GBA 0x080E9644 (0x24 = 36 bytes per entry), 113 entries
indexed by enemy id (Bat..Chaos) -- the same table :mod:`.soul_drop_rates` adjusts. Three bytes
of each entry describe the soul that enemy drops:

    +0x12  drop rate   -- chance ~= numerator / (rate*8 + 32 - LCK/16), so a LOWER byte is a
                         MORE common drop; 0 makes the death routine skip the roll (no drop)
    +0x17  soul type   -- 0 red/bullet, 1 blue/guardian, 2 yellow/enchant, 3 ability
    +0x18  soul index  -- 1-based within the type; 0 means the enemy drops no soul at all

The meanings of +0x17/+0x18 were recovered by correlating the table against
``data/item_info/item_info.csv``: all 110 soul-dropping entries resolve to exactly one named
soul, the mapping is 1:1 (no soul has two sources), and the three enemies
:mod:`.soul_drop_rates` already knows (43 Curly, 48 Devil, 54 Manticore) land on their
expected souls. The AoS decompilation leaves both bytes inside an undocumented pad, so this
module is the reference for them.

What gets shuffled
------------------
The 7 enemies whose soul is *progression* are never touched, so logic is untouched by
construction -- :mod:`..regions` only builds enemy regions for Flame Demon, Succubus, Devil and
Manticore, so freezing all 7 is strictly more conservative than logic requires. The remaining
103 enemies are permuted; because the vanilla mapping is 1:1, a permutation gives every soul
exactly one new source and leaves none unobtainable.

Drop rates
----------
Controlled by the ``keep_soul_drop_rates`` option:

* ``False`` -- the rate byte stays with the ENEMY, so a soul is as rare as whatever now drops it.
* ``True`` -- the rate byte moves WITH THE SOUL, so each soul keeps its vanilla rarity and only
  its source changes. One exception: an enemy whose vanilla rate is 0 keeps it. Rate 0 tells
  the death routine to skip the soul roll; vanilla puts it on the one-time bosses (Headhunter,
  Death, Legion, Balore), whose souls their own scripts award, so a real rate there would add a
  second, generic drop on top of the scripted one.

Generation is ROM-free, so the vanilla table below is committed data. :func:`build_writes`
verifies it against the base ROM before writing anything.
"""
from __future__ import annotations

from random import Random
from typing import Dict, List

from .._bytemaker_compat import offset_of, sizeof
from .address_space import gba_space
from .enemy_table import ENEMY_TABLE, EnemyDNA, field_offset, row_offset

# Derived from the shared enemy-table declaration (rom/enemy_table.py); kept as names for
# callers that compute offsets themselves.
ENEMY_TABLE_GBA = ENEMY_TABLE.addr
ENEMY_STRIDE = sizeof(EnemyDNA)
SOUL_RATE_OFF = offset_of(EnemyDNA, "soul_rate")
SOUL_TYPE_OFF = offset_of(EnemyDNA, "soul_type")
SOUL_INDEX_OFF = offset_of(EnemyDNA, "soul_index")

SOUL_TYPE_RED = 0
SOUL_TYPE_BLUE = 1
SOUL_TYPE_YELLOW = 2
SOUL_TYPE_ABILITY = 3

MODE_OFF = "off"
MODE_WITHIN_TYPE = "within_type"
MODE_ANY_TYPE = "any_type"

# --- The free starting soul -------------------------------------------------------------
# The intro hands Soma one Winged Skeleton soul. That is NOT an enemy drop: it is baked into
# the intro code as THUMB immediates, so the enemy-table shuffle above cannot reach it and the
# player would still start with a vanilla Winged Skeleton. The site (file 0x5D004) is:
#
#     movs r0, #0            <- soul type,  0-based   (STARTING_GRANT_TYPE_OFF)
#     movs r1, #0            <- soul index, 0-based   (STARTING_GRANT_INDEX_OFF)
#     movs r2, #1            <- amount
#     bl   SoulInventory_AddAmountToSoulTotal      (GBA 0x0803278C)
#     ldrb r0, [r4, #0xd]    <- equippedRedSoul     (STARTING_EQUIP_LOAD_OFF)
#     cmp  r0, #0
#     bne  +2                <- leave alone if something is already equipped
#     movs r0, #1            <- which soul to equip, 1-BASED  (STARTING_EQUIP_OFF)
#     strb r0, [r4, #0xd]    <- equippedRedSoul     (STARTING_EQUIP_STORE_OFF)
#
# Note the two different bases: the grant takes a 0-based index while the equip field is
# 1-based, so vanilla granting index 0 and equipping 1 name the same soul.
#
# The equip is not red-only by nature -- r4 is the 0x1325C player struct and the three
# equipped-soul fields are just consecutive displacements off it, so pointing the ldrb/strb
# at 0xE or 0xF equips the blue or yellow slot instead. That is what lets the starting soul
# be any type the shuffle produces.
STARTING_GRANT_TYPE_OFF = 0x05D004
STARTING_GRANT_INDEX_OFF = 0x05D006
STARTING_EQUIP_LOAD_OFF = 0x05D00E
STARTING_EQUIP_OFF = 0x05D014
STARTING_EQUIP_STORE_OFF = 0x05D016

# equipped<colour>Soul displacements within the 0x1325C player struct
# (EWRAM 0x13269/0x1326A/0x1326B).
EQUIP_SLOT_DISPLACEMENT = {
    SOUL_TYPE_RED: 0xD,
    SOUL_TYPE_BLUE: 0xE,
    SOUL_TYPE_YELLOW: 0xF,
}

# Expected vanilla bytes, verified before anything is written. The two halfwords are the
# whole instruction because the displacement lives in bits 6-10, not on a byte boundary.
STARTING_VANILLA_BYTES = {
    STARTING_GRANT_TYPE_OFF: bytes([0]),            # red
    STARTING_GRANT_INDEX_OFF: bytes([0]),           # Winged Skeleton
    STARTING_EQUIP_LOAD_OFF: bytes.fromhex("607b"),  # ldrb r0, [r4, #0xd]
    STARTING_EQUIP_OFF: bytes([1]),                 # equip red soul #1 == index 0
    STARTING_EQUIP_STORE_OFF: bytes.fromhex("6073"),  # strb r0, [r4, #0xd]
}


def _ldrb_r0_from_r4(displacement: int) -> bytes:
    """THUMB format-9 ``ldrb r0, [r4, #displacement]``."""
    return (0x7800 | (displacement << 6) | (4 << 3)).to_bytes(2, "little")


def _strb_r0_to_r4(displacement: int) -> bytes:
    """THUMB format-9 ``strb r0, [r4, #displacement]``."""
    return (0x7000 | (displacement << 6) | (4 << 3)).to_bytes(2, "little")

# enemy_id -> (soul type at +0x17, 1-based soul index at +0x18, drop rate at +0x12) in the
# unmodified USA ROM. Comments name the soul each row resolves to.
VANILLA: Dict[int, tuple[int, int, int]] = {
      0: (0,   2,   7),   # red 1 Bat
      1: (2,  12, 180),   # yellow 11 Zombie
      2: (0,   3,   8),   # red 2 Skeleton
      3: (0,   4,   7),   # red 3 Merman
      4: (0,   5,  30),   # red 4 Axe Armor
      5: (0,   6,  12),   # red 5 Skull Archer
      6: (2,   6, 120),   # yellow 5 Peeping Eye
      7: (0,   7,  15),   # red 6 Killer Fish
      8: (1,  10, 180),   # blue 9 Bone Pillar
      9: (0,   8,  30),   # red 7 Blue Crow
     10: (1,   4,  10),   # blue 3 Buer
     11: (2,  26,  10),   # yellow 25 White Dragon
     12: (0,   9,  12),   # red 8 Zombie Soldier
     13: (2,  22,  10),   # yellow 21 Skeleton Knight
     14: (0,  10,  20),   # red 9 Ghost
     15: (0,  11,  10),   # red 10 Siren
     16: (0,  12,  20),   # red 11 Tiny Devil
     17: (0,  13,  40),   # red 12 Durga
     18: (0,  14,  69),   # red 13 Rock Armor
     19: (1,   6,   8),   # blue 5 Giant Ghost
     20: (0,   1,  12),   # red 0 Winged Skeleton
     21: (2,  23,  15),   # yellow 22 Minotaur
     22: (0,  15,  20),   # red 14 Student Witch
     23: (0,  16,  10),   # red 15 Arachne
     24: (0,  17,  25),   # red 16 Fleaman
     25: (0,  18,   9),   # red 17 Evil Butcher
     26: (2,  27,  15),   # yellow 26 Quezlcoatl
     27: (2,  20, 150),   # yellow 19 Ectoplasm
     28: (1,   9,  50),   # blue 8 Catoblepas
     29: (2,  34,  10),   # yellow 33 Ghost Dancer
     30: (0,  19,  12),   # red 18 Waiter Skeleton
     31: (0,  51,  50),   # red 50 Killer Doll
     32: (2,   3,  30),   # yellow 2 Zombie Officer
     33: (1,  14,  40),   # blue 13 Creaking Skull
     34: (2,  11,  12),   # yellow 10 Wooden Golem
     35: (2,   9,   6),   # yellow 8 Tsuchinoko
     36: (1,  16,  50),   # blue 15 Persephone
     37: (2,  31,  15),   # yellow 30 Lilith
     38: (0,  52,  40),   # red 51 Nemesis
     39: (0,  54,  30),   # red 53 Kyoma Demon
     40: (0,  55,   6),   # red 54 Chronomage  <- progression
     41: (0,  46,  50),   # red 45 Valkyrie
     42: (1,   5,   7),   # blue 4 Witch
     43: (1,  20,  50),   # blue 19 Curly  <- progression
     44: (0,  20,  18),   # red 19 Altair
     45: (2,  30,  10),   # yellow 29 Red Crow
     46: (0,  22,  15),   # red 21 Cockatrice
     47: (2,   5,   8),   # yellow 4 Dead Warrior
     48: (1,  18,  32),   # blue 17 Devil  <- progression
     49: (1,  22,  15),   # blue 21 Imp
     50: (0,  23,  20),   # red 22 Werewolf
     51: (2,  28,  20),   # yellow 27 Gorgon
     52: (0,  30,  25),   # red 29 Disc Armor
     53: (2,  24,  20),   # yellow 23 Golem
     54: (1,  19,  32),   # blue 18 Manticore  <- progression
     55: (2,  35,  15),   # yellow 34 Gremlin
     56: (0,  24,  12),   # red 23 Harpy
     57: (1,  15, 120),   # blue 14 Medusa Head
     58: (0,  47,  60),   # red 46 Bomber Armor
     59: (0,  45,  56),   # red 44 Lightning Doll
     60: (1,   8,  30),   # blue 7 Great Armor
     61: (0,  25,  12),   # red 24 Une
     62: (2,  10,  20),   # yellow 9 Giant Worm
     63: (0,  26,  15),   # red 25 Needles
     64: (0,  27,  15),   # red 26 Man-Eater
     65: (0,  29,  12),   # red 28 Fish Head
     66: (0,  31,  20),   # red 30 Nightmare
     67: (2,  25,  25),   # yellow 24 Triton
     68: (0,  32,  12),   # red 31 Slime
     69: (1,  12,  15),   # blue 11 Big Golem
     70: (0,  33,  50),   # red 32 Dryad
     71: (2,  19, 100),   # yellow 18 Poison Worm
     72: (2,  18,  60),   # yellow 17 Arc Demon
     73: (1,  11,  20),   # blue 10 Cagnazzo
     74: (0,  34,  30),   # red 33 Ripper
     75: (0,  35,  20),   # red 34 Werejaguar
     76: (0,  28,  15),   # red 27 Ukoback
     77: (1,  17,  60),   # blue 16 Alura Une
     78: (0,  37,  20),   # red 36 Biphron
     79: (0,  38,  20),   # red 37 Mandragora
     80: (2,   8,  20),   # yellow 7 Flesh Golem
     81: (1,  21,   1),   # blue 20 Sky Fish
     82: (2,  29,  25),   # yellow 28 Dead Crusader
     83: (3,   4,   2),   # ability 3 Kicker Skeleton  <- progression
     84: (0,  36,  20),   # red 35 Weretiger
     85: (0,  53,  13),   # red 52 Killer Mantle
     86: (0,  21,  11),   # red 20 Mudman
     87: (2,  21, 180),   # yellow 20 Gargoyle
     88: (0,  48,  80),   # red 47 Red Minotaur
     89: (0,  39,  15),   # red 38 Beam Skeleton
     90: (1,  23,  25),   # blue 22 Alastor
     91: (0,  40,  10),   # red 39 Skull Millione
     92: (0,  41,  12),   # red 40 Giant Skeleton
     93: (0,  42,  10),   # red 41 Gladiator
     94: (2,  32,  20),   # yellow 31 Bael
     95: (2,   7,   5),   # yellow 6 Succubus  <- progression
     96: (2,  17,  30),   # yellow 16 Mimic
     97: (2,  33,  25),   # yellow 32 Stolas
     98: (2,  16, 120),   # yellow 15 Erinys
     99: (2,  13,  80),   # yellow 12 Lubicant
    100: (2,  15,  30),   # yellow 14 Basilisk
    101: (2,   4,  20),   # yellow 3 Iron Golem
    102: (0,  43,  30),   # red 42 Demon Lord
    103: (1,   7,  30),   # blue 6 Final Guard
    104: (0,  44,   3),   # red 43 Flame Demon  <- progression
    105: (1,  13,  20),   # blue 12 Shadow Knight
    106: (2,  14,   0),   # yellow 13 Headhunter
    107: (1,  24,   0),   # blue 23 Death
    108: (0,  49,   0),   # red 48 Legion
    109: (0,  50,   0),   # red 49 Balore
    110: (0,   0,   0),   # no soul
    111: (0,   0,   0),   # no soul
    112: (0,   0,   0),   # no soul
}

# Enemies whose vanilla soul is flagged ``progression`` in data/item_info/item_importance.csv.
# Frozen so that shuffling can never move a soul logic depends on. Derived from VANILLA plus
# that CSV; kept literal here because rom/ modules stay ROM-only leaves (data access lives in
# rom/patch.py).
PROGRESSION_SOUL_ENEMIES: frozenset[int] = frozenset([40, 43, 48, 54, 83, 95, 104])

# The enemy the intro's free soul belongs to: whichever one drops red index 0 (Winged
# Skeleton) in vanilla. Derived rather than hardcoded so it cannot drift from VANILLA.
STARTING_SOUL_ENEMY: int = next(
    eid for eid, (soul_type, index, _rate) in sorted(VANILLA.items())
    if soul_type == SOUL_TYPE_RED and index == 1)


def _entry(enemy_id: int) -> int:
    """File offset of ``enemy_id``'s row, addressed through the table's layout."""
    return row_offset(enemy_id)


def shuffleable_enemies(mode: str) -> List[int]:
    """Enemy ids eligible to have their soul reassigned under ``mode``, ascending."""
    if mode == MODE_OFF:
        return []
    return [eid for eid, (_type, index, _rate) in sorted(VANILLA.items())
            if index != 0 and eid not in PROGRESSION_SOUL_ENEMIES]


def plan_shuffle(random: Random, mode: str) -> Dict[int, int]:
    """
    Decide the shuffle: ``{target_enemy_id: source_enemy_id}``, meaning *target* now drops the
    soul that *source* dropped in vanilla.

    Reads only :data:`VANILLA`, so this runs at generation time without a base ROM. Enemies
    that keep their own soul are omitted, so the result is a permutation of its own key set.
    """
    eligible = shuffleable_enemies(mode)
    if not eligible:
        return {}

    if mode == MODE_ANY_TYPE:
        groups = [eligible]
    else:
        by_type: Dict[int, List[int]] = {}
        for eid in eligible:
            by_type.setdefault(VANILLA[eid][0], []).append(eid)
        groups = [group for _type, group in sorted(by_type.items())]

    plan: Dict[int, int] = {}
    for group in groups:
        sources = list(group)
        random.shuffle(sources)
        for target, source in zip(group, sources):
            if target != source:
                plan[target] = source
    return plan


def _verify_vanilla(base_rom: bytes) -> None:
    enemies = ENEMY_TABLE.bind(gba_space(base_rom))
    for eid, (soul_type, soul_index, rate) in VANILLA.items():
        row: EnemyDNA = enemies.item(eid).read()
        actual = (row.soul_type, row.soul_index, row.soul_rate)
        if actual != (soul_type, soul_index, rate):
            off = _entry(eid)
            raise ValueError(
                f"enemy {eid} soul bytes at {off:#x} are (type, index, rate)={actual}, "
                f"expected {(soul_type, soul_index, rate)} (ROM mismatch)")


def _validate_plan(plan: Dict[int, int]) -> None:
    if sorted(plan) != sorted(plan.values()):
        raise ValueError("soul-shuffle plan is not a permutation: it would duplicate or drop souls")
    for eid in plan:
        if eid in PROGRESSION_SOUL_ENEMIES:
            raise ValueError(f"soul-shuffle plan touches progression-soul enemy {eid}")
        if VANILLA[eid][1] == 0:
            raise ValueError(f"soul-shuffle plan targets enemy {eid}, which drops no soul")


def _verify_starting_grant(base_rom: bytes) -> None:
    for offset, expected in STARTING_VANILLA_BYTES.items():
        actual = base_rom[offset:offset + len(expected)]
        if actual != expected:
            raise ValueError(
                f"starting-soul site at {offset:#x} is {actual.hex()}, expected "
                f"{expected.hex()} (ROM mismatch)")


def _starting_soul_writes(plan: Dict[int, int]) -> Dict[int, bytes]:
    """
    Retarget the intro's free soul at whatever now drops from :data:`STARTING_SOUL_ENEMY`.

    Keeps the gift tied to the same enemy it came from in vanilla, so the intro stays
    consistent with the shuffled world, whatever type that soul turns out to be: the equip
    is repointed at the matching equipped-soul slot. Returns nothing when that enemy kept
    its own soul.
    """
    source = plan.get(STARTING_SOUL_ENEMY)
    if source is None:
        return {}

    soul_type, soul_index, _rate = VANILLA[source]
    if soul_type not in EQUIP_SLOT_DISPLACEMENT:
        # Only ability souls, and the sole ability-soul enemy is progression-frozen, so it can
        # never reach the shuffle pool. Fail loudly rather than emit a bad equip.
        raise ValueError(
            f"starting soul from enemy {source} has type {soul_type}, which has no equip slot")

    displacement = EQUIP_SLOT_DISPLACEMENT[soul_type]
    return {
        STARTING_GRANT_TYPE_OFF: bytes([soul_type]),
        STARTING_GRANT_INDEX_OFF: bytes([soul_index - 1]),   # grant is 0-based
        STARTING_EQUIP_LOAD_OFF: _ldrb_r0_from_r4(displacement),
        STARTING_EQUIP_OFF: bytes([soul_index]),             # equip field is 1-based
        STARTING_EQUIP_STORE_OFF: _strb_r0_to_r4(displacement),
    }


def build_writes(base_rom: bytes, plan: Dict[int, int], keep_soul_drop_rates: bool,
                 shuffle_starting_soul: bool = False) -> Dict[int, bytes]:
    """
    Writes reassigning enemy soul drops according to ``plan``.

    Args:
        base_rom: the original ROM bytes, verified against :data:`VANILLA` first.
        plan: ``{target_enemy_id: source_enemy_id}`` from :func:`plan_shuffle`.
        keep_soul_drop_rates: move each soul's vanilla rate along with it, except onto an
            enemy whose vanilla rate is 0 (the death routine skips the roll for those), which keeps 0.
        shuffle_starting_soul: also retarget the intro's free Winged Skeleton soul at
            whatever now drops from the enemy it belonged to.

    Returns the written dict.
    """
    _verify_vanilla(base_rom)
    _validate_plan(plan)

    # Built as a plain offset map rather than through a Patch: a Patch coalesces the adjacent
    # type/index bytes into one edit and omits a write whose value the ROM already holds, while
    # this module's contract (and patch.py's merge) is one entry per field, emitted always.
    writes: Dict[int, bytes] = {}
    for target, source in plan.items():
        soul_type, soul_index, soul_rate = VANILLA[source]
        writes[field_offset(target, "soul_type")] = bytes([soul_type])
        writes[field_offset(target, "soul_index")] = bytes([soul_index])
        if keep_soul_drop_rates and VANILLA[target][2] != 0:
            writes[field_offset(target, "soul_rate")] = bytes([soul_rate])

    if shuffle_starting_soul and plan:
        _verify_starting_grant(base_rom)
        writes.update(_starting_soul_writes(plan))
    return writes
