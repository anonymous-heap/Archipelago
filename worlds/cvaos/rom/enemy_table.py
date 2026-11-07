"""
The enemy stat table, declared once for the features that edit it.

The table is a flat array at GBA 0x080E9644 of 113 rows (Bat..Chaos), 0x24 bytes each.
``soul_drop_rates`` edits the +0x12 rate byte, ``soul_shuffle`` edits the +0x17 soul type, the
+0x18 soul index and the rate, and ``enemy_drop_rates`` edits the rate and the +0x15 rarity of the
common drop. They take the table's address, row size and field offsets from this declaration:
``EnemyDNA`` is the row layout, and ``ENEMY_TABLE`` places the rows in the ROM.
"""
from __future__ import annotations

from .._bytemaker_compat import Entry, Struct, UInt8, array, count, u8, u16
from .address_space import gba_space

ENEMY_COUNT = 113  # rows in the table (Bat..Chaos)


class EnemyDNA(Struct, endian="little"):
    """One 0x24-byte row of the enemy stat table.

    Only the bytes this world reads or edits are modelled. The ``unk_`` arrays cover the rest of
    the row, whose meaning is not mapped.

    Each row has two item drop slots. The death routine (``sub_080683BC``) rolls for an item only
    when no soul dropped. It picks slot 2 a quarter of the time, reading ``+0x0A`` and ``+0x16``
    at 0x0806857C, and slot 1 otherwise, reading ``+0x08`` and ``+0x15`` at 0x08068586. A slot
    that holds 0 drops no item. Otherwise its item drops with chance
    ``4 / max(16, rarity*4 + 16 - LCK/16)``, so a lower rarity is a more common drop, and a rarity
    of 0 is the most common one. The spawn routine (``sub_08044054``) subtracts 1 from the slot's
    value to get the item number (0x080440E6).
    """

    unk_00: list[int] = array(UInt8, 8)             # not yet mapped
    drop_1: u16                                     # +0x08: the common drop's item number plus 1,
                                                    # or 0 for none
    drop_2: u16                                     # +0x0A: the rare drop, encoded the same way
    unk_0C: list[int] = array(UInt8, 6)             # not yet mapped
    soul_rate: u8                                   # +0x12: drop rarity, where
                                                    # chance ~ n/(rate*8 + 32 - LCK/16)
    unk_13: list[int] = array(UInt8, 2)             # not yet mapped
    drop_1_rarity: u8                               # +0x15: the common drop's rarity
    drop_2_rarity: u8                               # +0x16: the rare drop's rarity
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
