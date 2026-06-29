"""
Neutralise the Forbidden Area press-button so the barrier can only be opened via the shuffled
"Study Sealswitch" item (the ForbiddenAreaButton == pickup option). The barrier object is untouched.

Background (verified against the USA ROM): room A01's object list -- pointed to by the room header at
0x08521510, +0x14 = 0x08521B8C -- is a flat array of 12-byte entities (type at +0x05: 2=object;
subtype at +0x06: 0x35 = metal-gate button/barrier; var_a u16 @ +0x08; var_b u16 @ +0x0A) terminated
by the first entry with type==0:

    0x08521B8C  press-button  (type=2 sub=0x35 var_a=0 var_b=48)  -- sets MISC flag #48 when pressed
    0x08521B98  barrier       (type=2 sub=0x35 var_a=1 var_b=48)  -- sinks while MISC #48 is set
    0x08521BA4  terminator    (type=0)

Rather than delete the entity (which would mean shifting the list / juggling the terminator), we
overwrite the button **in place** with an inert candle (subtype 0x07, var_a=var_b=0 -- a real,
game-handled config, e.g. the candle at 0x08510A80). The list length, terminator, and list pointer
are all unchanged; only the one entity changes from "press-button that sets MISC #48" to "decorative
candle". The barrier stays, so 20D->A02 is blocked until the Key sets MISC #48.
"""
from __future__ import annotations

from typing import Dict

from .entity import GBA_ROM_BASE

A01_BUTTON_ENTITY_GBA = 0x08521B8C        # object-0x35 var_b=48 press-button (A01 object list head)
ENTITY_SIZE = 12
_TYPE_OFF, _SUBTYPE_OFF, _VARA_OFF, _VARB_OFF = 0x05, 0x06, 0x08, 0x0A
_OBJECT_TYPE, _BUTTON_SUBTYPE, _BUTTON_VARB = 0x02, 0x35, 48
_CANDLE_SUBTYPE = 0x07                     # inert decorative object (drops nothing at var_a=var_b=0)


def build_writes(base_rom: bytes) -> Dict[int, bytes]:
    """``{file_offset: bytes}`` that turns the A01 press-button into an inert candle (in place).
    Raises if the button is not where expected, so a ROM mismatch fails loudly rather than silently
    editing the wrong entity."""
    off = A01_BUTTON_ENTITY_GBA - GBA_ROM_BASE
    e = bytearray(base_rom[off:off + ENTITY_SIZE])
    if not (e[_TYPE_OFF] == _OBJECT_TYPE and e[_SUBTYPE_OFF] == _BUTTON_SUBTYPE
            and int.from_bytes(e[_VARB_OFF:_VARB_OFF + 2], "little") == _BUTTON_VARB):
        raise ValueError(f"A01 forbidden-area button not at {A01_BUTTON_ENTITY_GBA:#x} (ROM mismatch)")
    # Keep x/y/id; make it a do-nothing candle. (type stays 2=object.)
    e[_SUBTYPE_OFF] = _CANDLE_SUBTYPE
    e[_VARA_OFF:_VARA_OFF + 2] = b"\x00\x00"
    e[_VARB_OFF:_VARB_OFF + 2] = b"\x00\x00"
    return {off: bytes(e)}
