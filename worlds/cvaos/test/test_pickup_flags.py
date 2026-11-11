"""
Offline checks for the collected-pickup flag bitfield (no emulator needed).

These check that every tracked flag_offset fits the bitfield, and that the client numbers its bits
LSB-first, as ``AoSRAM._flag_id`` documents. They do not confirm that the game numbers the bits
the same way, which needs a live check in the game.
"""
from __future__ import annotations

from ..data import pickup_info_collection
from ..ram import AoSRAM
from ..ram.addresses import PICKUP_FLAGS_LEN

_REGION_BITS = PICKUP_FLAGS_LEN * 8


def test_flag_offsets_fit_region():
    """Every tracked flag_offset addresses a bit inside the declared 0x02000360 region."""
    for p in pickup_info_collection:
        if p.flag_offset <= 0:
            continue
        assert 0 < p.flag_offset < _REGION_BITS, (
            f"{p.identifier_key}: flag_offset {p.flag_offset} outside 0..{_REGION_BITS - 1}")


def test_bits_are_numbered_lsb_first():
    """Bit 0 of byte 1 is flag 8. Under MSB-first numbering it would be flag 15."""
    assert AoSRAM.pickup_flag_ids(b"\x00\x01") == {8}
