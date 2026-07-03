"""
Single-Jump Divekick (the "Single Jump Divekick" option).

Port of the "Single Jump Divekick" patch from Xanthus 
(https://github.com/Xanthus1/aos_patches) with their generous permission.

Vanilla only allows the divekick out of a double jump (i.e. once Malphas is owned); this lets it
come out of a first jump too, with two guard rails kept from the original patch:

* no divekick while in water without Skula, and
* no divekick when there is ground immediately below (you cannot buffer one from the ground,
  which would otherwise stand in for the slide).

Three small edits inside the USA ROM's divekick-eligibility routine (GBA 0x08017D90 area):

* 0x08019320: ``bne`` that bails when the double jump has not been used -> ``nop``.
* 0x08017DB6: ``cmp r1, #4`` (has-double-jumped state) -> ``cmp r1, #2`` (is-midair state).
* 0x08017E18: the state-flag mask word the routine tests: ``0x00800004`` -> ``0x09000002``
  (require first-jump 0x2; exclude in-water 0x01000000 and ground-below 0x08000000).
"""
from __future__ import annotations

from typing import Dict

# (file_offset, expected current bytes, replacement bytes)
_EDITS: tuple[tuple[int, bytes, bytes], ...] = (
    (0x19320, bytes.fromhex("31d1"), bytes.fromhex("c046")),          # bne -> nop
    (0x17DB6, bytes.fromhex("042900d0"), bytes.fromhex("022900d0")),  # cmp r1,#4 -> cmp r1,#2
    (0x17E18, bytes.fromhex("04008000"), bytes.fromhex("02000009")),  # state-mask data word
)


def build_writes(base_rom: bytes) -> Dict[int, bytes]:
    """``{rom_file_offset: bytes}`` enabling the single-jump divekick. ``base_rom`` is the clean
    ROM, used to verify every overwritten byte still holds its expected vanilla value."""
    writes: Dict[int, bytes] = {}
    for offset, expected_old, new in _EDITS:
        current = base_rom[offset:offset + len(expected_old)]
        if current != expected_old:
            raise ValueError(
                f"single_jump_divekick: bytes at {offset:#x} are {current.hex()}, expected "
                f"{expected_old.hex()} (ROM mismatch)"
            )
        writes[offset] = new
    return writes
