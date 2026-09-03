"""
bytemaker structs for contiguous Aria of Sorrow RAM regions.

Where each block lives is declared in ``addresses.py``, as an ``Entry`` over the struct. All
values are little-endian. Fields read as plain ``int``; ``pack()`` / ``parse()`` convert to and
from the raw block.
"""
from __future__ import annotations

from .._bytemaker_compat import Struct, s16, sizeof, u4, u8, u16


class PlayerVitals(Struct, endian="little"):
    """
    HP/MP block at ``addresses.VITALS`` (0x0201327A).
    """

    current_hp: s16
    current_mp: s16
    max_hp: u16
    max_mp: u16


class EquippedGear(Struct, endian="little"):
    """
    Currently-equipped item/soul indices at ``addresses.GEAR`` (0x02013268).
    """

    weapon: u8
    red_soul: u8
    blue_soul: u8
    yellow_soul: u8
    armor: u8
    accessory: u8


class SoulPair(Struct, endian="little", bit_order="lsb"):
    """
    One byte of a nibble-packed soul array: two owned-counts per byte.

    ``even`` is the low nibble (the even-indexed soul) and ``odd`` the high nibble. That is the
    packing rule stated once as a declaration, so no call site shifts and masks.
    """

    even: u4
    odd: u4


# Byte sizes of each struct, for the read that precedes a parse.
VITALS_SIZE = sizeof(PlayerVitals)
EQUIPPED_GEAR_SIZE = sizeof(EquippedGear)
