"""
Which enemy drops which item in one player's seed, and how often.

Item Drop Shuffle's rules live in ``drop_shuffle.py``. This module applies them to a world.
:func:`travel_plan` reads the options that decide which drops move into the item pool, and
:func:`final_drop_table` decides the table that the patch writes (``rom/drop_table.py``).

The spoiler and the clients' /drops command print the same table, from :func:`drop_table_lines`.
/drops reads the enemy table from the ROM in the game's memory, so it shows what the installed
patch wrote. Generation has no ROM, so :func:`planned_drop_table_lines` runs the patch's own drop
edits on a stand-in that holds only the vanilla drop fields, as ``soul_sources`` does for souls.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Mapping, Optional, Tuple

from .data import enemy_type_collection
from .data.item_info import by_item_number
from .drop_shuffle import DropSlot, TravelPlan, plan_drop_shuffle, plan_travel
from .rom.drop_table import MAX_VALUE, vanilla_bytes
from .rom.enemy_table import TABLE_OFFSET, TABLE_SIZE, parse_table
from .seed_depth import enemy_seed_depths
from .soul_sources import ROM_DOMAIN

if TYPE_CHECKING:
    from . import CVAOSWorld
    from .ram import AoSRAM

__all__ = [
    "DropTable",
    "drop_table_lines",
    "final_drop_table",
    "planned_drop_table_lines",
    "read_drop_table_report",
    "rom_drop_table_lines",
    "spoiler_drop_table_lines",
    "travel_plan",
]

# Each enemy id's two drop slots, common first.
DropTable = Mapping[int, Tuple[DropSlot, DropSlot]]

# The item roll runs on a kill that drops no soul. It picks the common slot three times in four
# and the rare slot otherwise, then drops the slot's item with chance
# 4 / max(16, rarity*4 + 16 - LCK/16) (rom/enemy_table.py).
_SLOT_SHARES = (3 / 4, 1 / 4)
_ITEM_NUMERATOR = 4

TABLE_NOTE = "Chances are per kill that drops no soul, before luck and the Rare Ring."


def _wide_item_smoothing(world: CVAOSWorld) -> bool:
    """Whether Wide Item Smoothing is on, which moves more drops into the pool."""
    # Wide Item Smoothing has no option yet, so it is always off.
    return False


def travel_plan(world: CVAOSWorld) -> Optional[TravelPlan]:
    """Return which drops move into ``world``'s item pool, or None with Item Drop Shuffle off.

    The plan depends only on options that the slot data carries, so Universal Tracker's rebuild of
    the world gets the same pool.
    """
    options = world.options
    if not options.item_drop_shuffle:
        return None
    return plan_travel(options.enemy_checks.value, options.goal.value, _wide_item_smoothing(world))


def final_drop_table(world: CVAOSWorld) -> Optional[DropTable]:
    """Return the drop table that ``world``'s patch writes, or None to keep the game's own.

    With Item Drop Shuffle on, it is the table that the shuffle deals by seed depth. This is the
    one place that decides the final table, so any later step that changes drops belongs here.

    Reads the seed depths, so call it only after progression balancing.
    """
    options = world.options
    if not options.item_drop_shuffle:
        return None
    plan = plan_drop_shuffle(options.enemy_checks.value, options.goal.value,
                             _wide_item_smoothing(world), enemy_seed_depths(world))
    return plan.table


def _item_name(value: int) -> str:
    """Return the name of the item that a drop slot's stored value spawns."""
    info = by_item_number.get(value - 1) if value <= MAX_VALUE else None
    return info.name if info is not None else f"unknown item {value:#x}"


def _chance(rarity: int, share: float) -> str:
    """Describe the chance of a drop with this rarity byte, in a slot the roll picks ``share`` of
    the time."""
    kills = max(16, rarity * 4 + 16) / (_ITEM_NUMERATOR * share)
    return f"1 in {kills:.3g} kills"


def drop_table_lines(table: bytes) -> List[str]:
    """Return the drop table as lines of text.

    ``table`` is the enemy table's TABLE_SIZE bytes. Each enemy that drops an item gets one line, in
    enemy id order, which is the order of the game's Enemy List.
    """
    lines = [TABLE_NOTE]
    for enemy, row in zip(enemy_type_collection, parse_table(table)):
        slots = ((row.drop_1, row.drop_1_rarity, "common", _SLOT_SHARES[0]),
                 (row.drop_2, row.drop_2_rarity, "rare", _SLOT_SHARES[1]))
        drops = [f"{_item_name(value)} ({kind}, {_chance(rarity, share)})"
                 for value, rarity, kind, share in slots if value]
        if drops:
            lines.append(f"{enemy.name}: {', '.join(drops)}")
    return lines


def rom_drop_table_lines(rom: bytes) -> List[str]:
    """Return the drop table of a whole ROM image."""
    return drop_table_lines(rom[TABLE_OFFSET:TABLE_OFFSET + TABLE_SIZE])


async def read_drop_table_report(ram: AoSRAM) -> str:
    """Return the drop table of the ROM in the game's memory, as /drops prints it."""
    table = await ram.read(TABLE_OFFSET, TABLE_SIZE, ROM_DOMAIN)
    return "\n".join(["Item drops:", *(f"    {line}" for line in drop_table_lines(table))])


def planned_drop_table_lines(rom_config: Dict[str, object]) -> List[str]:
    """Return the drop table that a patch with ``rom_config`` writes, without the base ROM.

    The patch's own drop edits run on a stand-in that holds only the vanilla drop fields. Those
    edits read nothing else, so the stand-in gives the same drop bytes as the base ROM would.
    """
    from .rom.patch import apply_drop_rate_edits, apply_drop_table_edits

    stand_in = bytearray(TABLE_OFFSET + TABLE_SIZE)
    for offset, data in vanilla_bytes().items():
        stand_in[offset:offset + len(data)] = data
    apply_drop_table_edits(stand_in, rom_config)
    apply_drop_rate_edits(stand_in, rom_config)
    return rom_drop_table_lines(bytes(stand_in))


def spoiler_drop_table_lines(world: CVAOSWorld) -> List[str]:
    """Return the drop table for ``world``'s spoiler, or no lines when the seed changes neither
    which enemy drops which item nor how often."""
    from .rom.patch import seed_rom_config

    config = seed_rom_config(world)
    if not (config.get("drop_table") or config.get("enemy_drop_rates")):
        return []
    return planned_drop_table_lines(config)
