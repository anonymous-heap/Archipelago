"""
Oops! All Whips (the "Oops All Whips" option).

Port of the "Oops All Whips" patch from Xanthus 
(https://github.com/Xanthus1/aos_patches) with their generous permission.

Every weapon attacks with the Whip Sword's swing animation.

The edits all land in the USA ROM's weapon attribute table (first touched entry at 0x08505D4D,
0x1C bytes per weapon, ~58 entries edited). Per weapon the patch forces the two attack-animation-id
bytes to 0x22 (the Whip Sword's animation), sets bit 0x20 in the adjacent animation-flag byte, and
zeroes two animation-category bytes. Weapon stats/hitboxes are untouched, but effective reach
follows the whip swing, so feel (and some incidental enemy hits) will differ.

No logic impact: logic only ever requires *having* an attack, not a specific animation.

``_RECORDS`` is byte-for-byte from Xanthus's distributed patch.
"""
from __future__ import annotations

from typing import Dict

# (file_offset, expected current bytes hex, replacement bytes hex)
_RECORDS: tuple[tuple[int, str, str], ...] = (
    (0x505D4D, "000404000001", "202222000000"),
    (0x505D69, "000404000101", "202222000100"),
    (0x505D85, "000505000002", "202222000000"),
    (0x505DA1, "000606000001", "202222000000"),
    (0x505DBD, "000000000001", "202222000000"),
    (0x505DF5, "000000000101", "202222000100"),
    (0x505E11, "000a0a000002", "202222000000"),
    (0x505E2D, "010000000201", "212222000200"),
    (0x505E49, "000909000401", "202222000400"),
    (0x505E65, "000a0a000102", "202222000100"),
    (0x505E81, "000808000001", "202222000000"),
    (0x505E9D, "000808000201", "202222000200"),
    (0x505EB9, "000909000301", "202222000300"),
    (0x505ED5, "002c2c000901", "202222000900"),
    (0x505EF1, "002d2d000901", "202222000900"),
    (0x505F0D, "000b0b000001", "202222000000"),
    (0x505F2A, "0707", "2222"),
    (0x505F3C, "01", "00"),
    (0x505F46, "0101000001", "2222000000"),
    (0x505F58, "01", "00"),
    (0x505F62, "2929000001", "2222000000"),
    (0x505F74, "01", "00"),
    (0x505F7E, "2b2b000101", "2222000100"),
    (0x505F90, "01", "00"),
    (0x505F9A, "0101000101", "2222000100"),
    (0x505FAC, "01", "00"),
    (0x505FB6, "2b2b000601", "2222000600"),
    (0x505FC8, "01", "00"),
    (0x505FD2, "0c0c000002", "2222000000"),
    (0x505FE4, "01", "00"),
    (0x505FEE, "0202000101", "2222000100"),
    (0x506000, "01", "00"),
    (0x506009, "000303000001", "202222000000"),
    (0x50601C, "01", "00"),
    (0x506026, "2121", "2222"),
    (0x506038, "01", "00"),
    (0x506041, "000e0e000002", "202222000000"),
    (0x506054, "01", "00"),
    (0x50605D, "000d0d000102", "202222000100"),
    (0x506070, "01", "00"),
    (0x506079, "000f0f000002", "202222000000"),
    (0x50608C, "01", "00"),
    (0x506095, "000d0d000002", "202222000000"),
    (0x5060A8, "01", "00"),
    (0x5060B1, "000d0d000502", "202222000500"),
    (0x5060C4, "01", "00"),
    (0x5060CD, "000d0d000302", "202222000300"),
    (0x5060E0, "01", "00"),
    (0x5060E9, "000d0d000402", "202222000400"),
    (0x5060FC, "01", "00"),
    (0x506106, "1010", "2222"),
    (0x506118, "01", "00"),
    (0x506122, "1111", "2222"),
    (0x506134, "01", "00"),
    (0x50613E, "1a1a", "2222"),
    (0x506150, "01", "00"),
    (0x50615A, "1212", "2222"),
    (0x50616C, "01", "00"),
    (0x506176, "1313", "2222"),
    (0x506188, "01", "00"),
    (0x506192, "2727", "2222"),
    (0x5061A4, "02", "00"),
    (0x5061AD, "001414000001", "202222000000"),
    (0x5061C0, "02", "00"),
    (0x5061C9, "001414000001", "202222000000"),
    (0x5061DC, "02", "00"),
    (0x5061E5, "001616000001", "202222000000"),
    (0x5061F8, "02", "00"),
    (0x506202, "1515", "2222"),
    (0x506214, "02", "00"),
    (0x50621E, "1b1b", "2222"),
    (0x506230, "02", "00"),
    (0x50623A, "1c1c", "2222"),
    (0x50624C, "02", "00"),
    (0x506256, "1d1d", "2222"),
    (0x506268, "02", "00"),
    (0x506272, "1e1e", "2222"),
    (0x506284, "02", "00"),
    (0x50628E, "1f1f", "2222"),
    (0x5062A0, "02", "00"),
    (0x5062AA, "2020", "2222"),
    (0x5062BC, "03", "00"),
    (0x5062C5, "002323", "202222"),
    (0x5062D8, "03", "00"),
    (0x5062E1, "002424", "202222"),
    (0x5062F4, "03", "00"),
    (0x5062FD, "002a2a", "202222"),
    (0x506310, "03", "00"),
    (0x506319, "002525", "202222"),
    (0x50632C, "03", "00"),
    (0x506335, "002626", "202222"),
    (0x506348, "04", "00"),
    (0x506351, "101919", "302222"),
    (0x506364, "04", "00"),
    (0x50636D, "001919", "202222"),
    (0x506380, "02", "00"),
    (0x50638A, "2828", "2222"),
    (0x50639C, "05", "00"),
    (0x5063A5, "002e2e", "202222"),
)


def build_writes(base_rom: bytes) -> Dict[int, bytes]:
    """``{rom_file_offset: bytes}`` retargeting every weapon's attack animation to the Whip
    Sword's. ``base_rom`` is the clean ROM, used to verify every overwritten byte still holds its
    expected vanilla value."""
    writes: Dict[int, bytes] = {}
    for offset, old_hex, new_hex in _RECORDS:
        expected_old = bytes.fromhex(old_hex)
        current = base_rom[offset:offset + len(expected_old)]
        if current != expected_old:
            raise ValueError(
                f"oops_all_whips: bytes at {offset:#x} are {current.hex()}, expected "
                f"{expected_old.hex()} (ROM mismatch)"
            )
        writes[offset] = bytes.fromhex(new_hex)
    return writes
