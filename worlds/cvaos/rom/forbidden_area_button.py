"""
Neutralize the Forbidden Area press-button so the barrier can only be opened via the shuffled
"Study Sealswitch" item (the ForbiddenAreaButton == pickup option). The barrier object is untouched.

Background (verified against the USA ROM): room A01's object list is a flat array of 12-byte
entities, terminated by the first entry with type 0. The room header at 0x08521510 holds the
list's address, 0x08521B8C, at +0x14. In each entity, the type is at +0x05 (2 = object), the
subtype at +0x06 (0x35 = metal-gate button/barrier), var_a is a u16 at +0x08, and var_b is a u16
at +0x0A. The list holds:

    0x08521B8C  press-button  (type=2 sub=0x35 var_a=0 var_b=48): sets MISC flag #48 when pressed
    0x08521B98  barrier       (type=2 sub=0x35 var_a=1 var_b=48): sinks while MISC #48 is set
    0x08521BA4  terminator    (type=0)

The button is overwritten **in place** with an object that does nothing: subtype 0x07 with
var_a=4 and var_b=0. Object07Create does nothing for that pair, and Object07Update ignores
var_a 4. Deleting the entry instead would mean shifting the rest of the list and moving the
terminator.

Other var_a values are not inert. With var_a 0 the object is Castle Corridor's ambient particles:
it spawns effect entities and forces alpha blending every frame. With var_a 1 it animates a
palette.

The list length, terminator, and list pointer are all unchanged. The barrier stays, so 20D->A02 is
blocked until the Study Sealswitch sets MISC #48.
"""
from __future__ import annotations

from typing import Dict

from .entity import ENTITY_SIZE, GBA_ROM_BASE, AoSPickupEntity

A01_BUTTON_ENTITY_GBA = 0x08521B8C        # object-0x35 var_b=48 press-button (A01 object list head)
_OBJECT_TYPE, _BUTTON_SUBTYPE, _BUTTON_VARB = 0x02, 0x35, 48
_INERT_SUBTYPE, _INERT_VARA = 0x07, 4      # object 07 does nothing at var_a=4, var_b=0


def build_writes(base_rom: bytes) -> Dict[int, bytes]:
    """``{file_offset: bytes}`` that turn the A01 press-button into an inert object (in place).
    Raises ``ValueError`` if the button is not where expected, so a ROM mismatch fails loudly
    rather than silently editing the wrong entity."""
    off = A01_BUTTON_ENTITY_GBA - GBA_ROM_BASE
    e = AoSPickupEntity.parse(base_rom[off:off + ENTITY_SIZE])
    if not (e.type == _OBJECT_TYPE and e.subtype == _BUTTON_SUBTYPE and e.var_b == _BUTTON_VARB):
        raise ValueError(f"A01 forbidden-area button not at {A01_BUTTON_ENTITY_GBA:#x} (ROM mismatch)")
    # Keep x/y/id and type 2 (object); only what the object does changes.
    e.subtype = _INERT_SUBTYPE
    e.var_a = _INERT_VARA
    e.var_b = 0
    return {off: e.pack()}
