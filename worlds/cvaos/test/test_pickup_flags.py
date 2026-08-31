"""
Offline invariants for the collected-pickup flag bitfield (no emulator needed).

These guard the data pipeline plus the LSB-first bit-numbering convention in ``AoSRAM._flag_id``
against regressions. They do *not* confirm the hardware bit order (that is the one-time live
save-scum collect, still to be done) -- the order itself is documented in ``_flag_id``;
here we only assert internal consistency (range, bijection, and a single-bit round-trip).

NOTE: imports ``.ram``, so this test belongs with the (currently unpushed) ``ram/`` package.
"""
from __future__ import annotations

from ..data import pickup_info_collection
from ..locations import flag_offset_to_location_id
from ..ram import AoSRAM
from ..ram.addresses import PICKUP_FLAGS_LEN

_REGION_BITS = PICKUP_FLAGS_LEN * 8


def _buffer_with_bit(flag_id: int) -> bytes:
    """A PICKUP_FLAGS_LEN-byte buffer with exactly ``flag_id`` set, per the client's convention."""
    buf = bytearray(PICKUP_FLAGS_LEN)
    buf[flag_id >> 3] |= 1 << (flag_id & 7)
    return bytes(buf)


def test_flag_offsets_fit_region():
    """Every tracked flag_offset addresses a bit inside the declared 0x02000360 region."""
    for p in pickup_info_collection:
        if p.flag_offset <= 0:
            continue
        assert 0 < p.flag_offset < _REGION_BITS, (
            f"{p.identifier_key}: flag_offset {p.flag_offset} outside 0..{_REGION_BITS - 1}")


def test_flag_offset_map_is_bijective():
    """flag_offset -> ptr_address is 1:1 and covers exactly the tracked flag_offsets."""
    tracked = {p.flag_offset for p in pickup_info_collection if p.flag_offset > 0}
    assert set(flag_offset_to_location_id) == tracked
    ids = list(flag_offset_to_location_id.values())
    assert len(ids) == len(set(ids)), "two flag_offsets map to the same location id"


def test_single_bit_roundtrips_to_its_flag_id():
    """Setting only bit N in the bitfield decodes back to exactly {N} and the right location."""
    for p in pickup_info_collection:
        if p.flag_offset <= 0:
            continue
        decoded = AoSRAM.pickup_flag_ids(_buffer_with_bit(p.flag_offset))
        assert decoded == {p.flag_offset}, (
            f"{p.identifier_key}: bit {p.flag_offset} decoded to {decoded}")
        assert flag_offset_to_location_id[p.flag_offset] == p.ptr_address
