"""
The enemy stat table, declared once for the features that edit it.

The table is a flat array at GBA 0x080E9644 of 113 rows (Bat..Chaos), 0x24 bytes each.
``soul_drop_rates`` edits the +0x12 rate byte, and ``soul_shuffle`` edits the +0x17 soul type,
the +0x18 soul index and the rate. Both take the table's address, row size and field offsets
from this declaration: ``EnemyDNA`` is the row layout, and ``ENEMY_TABLE`` places the rows in
the ROM.
"""
from __future__ import annotations

from .._bytemaker_compat import Entry, Struct, UInt8, array, count, u8
from .address_space import gba_space

ENEMY_COUNT = 113  # rows in the table (Bat..Chaos)


class EnemyDNA(Struct, endian="little"):
    """One 0x24-byte row of the enemy stat table.

    Only the bytes this world edits are modelled. The ``unk_`` arrays cover the rest of the row,
    whose meaning is not mapped.
    """

    unk_00: list[int] = array(UInt8, 0x12)          # not yet mapped
    soul_rate: u8                                   # +0x12: drop rarity, where
                                                    # chance ~ n/(rate*8 + 32 - LCK/16)
    unk_13: list[int] = array(UInt8, 4)             # not yet mapped
    soul_type: u8                                   # +0x17: 0 red, 1 blue, 2 yellow, 3 ability
    soul_index: u8                                  # +0x18: 1-based index within the type,
                                                    # or 0 for no soul
    unk_19: list[int] = array(UInt8, 0x24 - 0x19)   # not yet mapped


ENEMY_TABLE = Entry(0x080E9644, EnemyDNA, count(ENEMY_COUNT), name="enemy table")

# Address math only: the table bound to the cart's geometry, no image behind it.
_TABLE_GEOMETRY = ENEMY_TABLE.bind(gba_space())


def row_offset(enemy_id: int) -> int:
    """File offset of ``enemy_id``'s row."""
    return _TABLE_GEOMETRY.item(enemy_id).request().offset


def field_offset(enemy_id: int, field: str) -> int:
    """File offset of one field of ``enemy_id``'s row, addressed through the layout."""
    return _TABLE_GEOMETRY.item(enemy_id).field(field).request().offset
