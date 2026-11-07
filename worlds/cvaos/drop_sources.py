"""
Which enemy drops which item in one player's seed, and how often.

``drop_shuffle.final_drop_table`` decides the table that the patch writes
(``rom/drop_table.py``), and this module prints it.

The spoiler and the clients' /drops command print the same table, from :func:`drop_table_lines`.
/drops reads the enemy table from the ROM in the game's memory, so it shows what the installed
patch wrote. Generation has no ROM, so :func:`planned_drop_table_lines` runs the patch's own drop
edits on a stand-in that holds only the vanilla drop fields, as ``soul_sources`` does for souls.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List

from .data import enemy_type_collection
from .data.item_info import by_item_number
from .ram.addresses import ROM
from .rom.drop_table import MAX_VALUE, vanilla_bytes
from .rom.enemy_table import TABLE_OFFSET, TABLE_SIZE, parse_table

if TYPE_CHECKING:
    from . import CVAOSWorld
    from .ram import AoSRAM

__all__ = [
    "drop_table_lines",
    "planned_drop_table_lines",
    "read_drop_table_report",
    "rom_drop_table_lines",
    "spoiler_drop_table_lines",
]

# The item roll runs on a kill that drops no soul. It picks the common slot three times in four
# and the rare slot otherwise, then drops the slot's item with chance
# 4 / max(16, rarity*4 + 16 - LCK/16) (rom/enemy_table.py).
_SLOT_SHARES = (3 / 4, 1 / 4)
_ITEM_NUMERATOR = 4

TABLE_NOTE = "Chances are per kill that drops no soul, before luck and the Rare Ring."


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
    table = await ram.read(TABLE_OFFSET, TABLE_SIZE, ROM)
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
