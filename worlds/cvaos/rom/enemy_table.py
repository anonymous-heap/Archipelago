"""
The enemy stat table, declared once.

A flat array at GBA 0x080E9644 of 113 rows (Bat..Chaos), 0x24 bytes each. Two features edit
it -- ``soul_drop_rates`` (the +0x12 rate byte) and ``soul_shuffle`` (the +0x17 soul type,
+0x18 soul index, and the rate) -- and each used to carry its own copy of the address, the
stride and the field offsets. They share this declaration instead: ``EnemyDNA`` is the row,
``ENEMY_TABLE`` places it, and ``offset_of`` / ``sizeof`` answer the numbers the features used
to state by hand.
"""
from __future__ import annotations

from .._bytemaker_compat import Entry, Struct, UInt8, array, count, u8
from .address_space import gba_space

ENEMY_COUNT = 113  # rows in the table (Bat..Chaos)


class EnemyDNA(Struct, endian="little"):
    """One 0x24-byte row of the enemy stat table.

    Only the bytes this world edits are modelled; the padding names the unmapped remainder
    rather than pretending to know it.
    """

    unk_00: list[int] = array(UInt8, 0x12)          # not yet mapped
    soul_rate: u8                                   # +0x12: drop rarity; chance ~ n/(rate*8 + 32 - LCK/16)
    unk_13: list[int] = array(UInt8, 4)             # not yet mapped
    soul_type: u8                                   # +0x17: 0 red, 1 blue, 2 yellow, 3 ability
    soul_index: u8                                  # +0x18: 1-based index within the type; 0 = no soul
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
