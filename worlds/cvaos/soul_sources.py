"""
Which enemy drops which soul, and how often.

The enemy table names each enemy's soul by two bytes: its type (0 red, 1 blue, 2 yellow,
3 ability) and its 1-based index within that type (``rom/soul_shuffle.py``). ``item_info.csv``
lists the same souls by category and 0-based id, so :func:`soul_name` converts the table's bytes
into a soul's name.

The spoiler and the clients' /souls command print the same table, from :func:`soul_table_lines`.
/souls reads the enemy table from the ROM in the game's memory, so it shows what the installed
patch wrote. Generation has no ROM, so :func:`planned_soul_table_lines` runs the patch's own soul
edits on a stand-in that holds only the vanilla soul bytes.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List

from .data import enemy_type_collection
from .data.item_info import by_category_id
from .rom.enemy_table import TABLE_OFFSET, TABLE_SIZE, field_offset, parse_table
from .rom.soul_shuffle import (SOUL_TYPE_ABILITY, SOUL_TYPE_BLUE, SOUL_TYPE_RED, SOUL_TYPE_YELLOW,
                               STARTING_GRANT_INDEX_OFF, STARTING_GRANT_TYPE_OFF,
                               STARTING_VANILLA_BYTES, VANILLA)

if TYPE_CHECKING:
    from . import CVAOSWorld
    from .ram import AoSRAM

__all__ = [
    "SOUL_CATEGORIES",
    "planned_soul_table_lines",
    "read_soul_table_report",
    "rom_soul_table_lines",
    "soul_name",
    "soul_table_lines",
    "spoiler_soul_table_lines",
    "vanilla_soul_name",
]

# Each soul type byte, with the item_info.csv category of its souls.
SOUL_CATEGORIES = {
    SOUL_TYPE_RED: "red_soul",
    SOUL_TYPE_BLUE: "blue_soul",
    SOUL_TYPE_YELLOW: "yellow_soul",
    SOUL_TYPE_ABILITY: "ability_soul",
}

ROM_DOMAIN = "ROM"  # the memory domain whose offsets are ROM file offsets (ram/backend.py)

# The intro's starting-soul grant: the immediates of ``movs r0, #type`` and ``movs r1, #index``,
# with the index 0-based (rom/soul_shuffle.py).
GRANT_OFFSET = STARTING_GRANT_TYPE_OFF
GRANT_SIZE = STARTING_GRANT_INDEX_OFF - STARTING_GRANT_TYPE_OFF + 1

# The death routine's soul chance is numerator / max(16, rate*8 + 32 - LCK/16). The numerator is
# 6 for a soul the player doesn't own yet and 3 for one they own.
_NEW_SOUL_NUMERATOR = 6

TABLE_NOTE = ("Chances are per kill, for a soul you don't own yet, before luck, Hard Mode, the "
              "Soul Eater Ring and the Ancient Books. A soul you own drops half as often.")


def soul_name(soul_type: int, soul_index: int) -> str:
    """Return the name of the soul that a table row's type byte and 1-based index byte name.

    Raises KeyError for an index of 0, which means the enemy drops no soul.
    """
    return by_category_id[(SOUL_CATEGORIES[soul_type], soul_index - 1)].name


def vanilla_soul_name(enemy_id: int) -> str | None:
    """Return the name of the soul that ``enemy_id`` drops in vanilla, or None if it drops none."""
    soul_type, soul_index, _rate = VANILLA[enemy_id]
    return soul_name(soul_type, soul_index) if soul_index else None


def _colour(soul_type: int) -> str:
    return SOUL_CATEGORIES[soul_type].removesuffix("_soul")


def _chance(rate: int) -> str:
    """Describe the chance that a soul rate byte gives."""
    if rate == 0:
        # Only the four scripted bosses hold a soul at rate 0. The death routine skips the roll,
        # and the boss's own script awards the soul.
        return "awarded when defeated"
    kills = max(16, rate * 8 + 32) / _NEW_SOUL_NUMERATOR
    return f"1 in {kills:.3g} kills"


def soul_table_lines(table: bytes, grant: bytes) -> List[str]:
    """Return the soul table as lines of text.

    ``table`` is the enemy table's TABLE_SIZE bytes, and ``grant`` is the GRANT_SIZE bytes of the
    intro's starting-soul grant. Each enemy that holds a soul gets one line, in enemy id order,
    which is the order of the game's Enemy List. The last line names the starting soul.
    """
    lines = [TABLE_NOTE]
    for enemy, row in zip(enemy_type_collection, parse_table(table)):
        if row.soul_index:
            soul = soul_name(row.soul_type, row.soul_index)
            lines.append(f"{enemy.name}: {soul} soul ({_colour(row.soul_type)}), "
                         f"{_chance(row.soul_rate)}")
    start_type, start_index = grant[0], grant[GRANT_SIZE - 1] + 1
    lines.append(f"Starting soul: {soul_name(start_type, start_index)} soul "
                 f"({_colour(start_type)})")
    return lines


def rom_soul_table_lines(rom: bytes) -> List[str]:
    """Return the soul table of a whole ROM image."""
    return soul_table_lines(rom[TABLE_OFFSET:TABLE_OFFSET + TABLE_SIZE],
                            rom[GRANT_OFFSET:GRANT_OFFSET + GRANT_SIZE])


async def read_soul_table_report(ram: AoSRAM) -> str:
    """Return the soul table of the ROM in the game's memory, as /souls prints it."""
    table = await ram.read(TABLE_OFFSET, TABLE_SIZE, ROM_DOMAIN)
    grant = await ram.read(GRANT_OFFSET, GRANT_SIZE, ROM_DOMAIN)
    return "\n".join(["Soul sources:", *(f"    {line}" for line in soul_table_lines(table, grant))])


def planned_soul_table_lines(rom_config: Dict[str, object]) -> List[str]:
    """Return the soul table that a patch with ``rom_config`` writes, without the base ROM.

    The patch's own soul edits run on a stand-in that holds only the vanilla table's soul bytes and
    the intro's starting-soul site. Those edits read nothing else, so the stand-in gives the same
    soul bytes as the base ROM would.
    """
    from .rom.patch import apply_drop_rate_edits, apply_soul_edits

    stand_in = bytearray(TABLE_OFFSET + TABLE_SIZE)
    for enemy_id, (soul_type, soul_index, rate) in VANILLA.items():
        stand_in[field_offset(enemy_id, "soul_type")] = soul_type
        stand_in[field_offset(enemy_id, "soul_index")] = soul_index
        stand_in[field_offset(enemy_id, "soul_rate")] = rate
    for offset, vanilla in STARTING_VANILLA_BYTES.items():
        stand_in[offset:offset + len(vanilla)] = vanilla
    apply_soul_edits(stand_in, rom_config)
    apply_drop_rate_edits(stand_in, rom_config)
    return rom_soul_table_lines(bytes(stand_in))


def spoiler_soul_table_lines(world: CVAOSWorld) -> List[str]:
    """Return the soul table for ``world``'s spoiler, or no lines when the seed changes neither
    which enemy drops which soul nor how often."""
    from .rom.patch import seed_rom_config

    config = seed_rom_config(world)
    multiplied = config["multiply_soul_drop_rates"] and config["soul_drop_rate_multiplier"] != 100
    if not (config["soul_shuffle_plan"] or config.get("enemy_drop_rates") or multiplied):
        return []
    return planned_soul_table_lines(config)
