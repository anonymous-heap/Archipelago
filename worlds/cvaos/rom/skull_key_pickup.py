"""
Let Soma collect a Skull Key pickup while he already holds 9 Skull Keys.

Every pickup that holds another world's item appears as a Skull Key (rom/patch.py
``_AP_PLACEHOLDER``). The client detects the location check through the pickup's save flag, so
the pickup has to be collectable at any Skull Key count.

AoS refuses a consumable pickup while the player holds 9 of that item. In the consumable branch of
the collect routine ``sub_080441F8``, 0x0804430E reads the owned count and 0x08044312 compares it
with 8. A higher count leads to the refusal at 0x0804443C, which plays sound 0x13B and clears the
despawn bit that the routine set on entry. The refusal then returns through 0x08044584, so the
finish at 0x08044508, which writes the pickup's save flag, never runs. The pickup stays on the
floor and no check is made.

Vanilla makes one exception at the cap: a pickup whose entity byte +0x0B has bit 1 set is
collected without being counted. The 10 bytes at 0x08044316 test that bit::

    ldrb r1, [r4, #0xb] ; movs r0, #2 ; ands r0, r1 ; cmp r0, #0 ; bne 0x08044322

This patch replaces them with a test that also accepts consumable 25, the Skull Key::

    cmp r2, #25 ; beq 0x08044322 ; ldrb r1, [r4, #0xb] ; lsls r1, r1, #30 ; bmi 0x08044322

At this point r2 holds the pickup's item offset. The ``lsls`` moves bit 1 into the sign bit, so
the ``bmi`` branches exactly when the vanilla ``bne`` did. Both destinations overwrite r0 and r1
before reading them, so neither depends on the values the vanilla test left there. The collect
path at 0x08044322 adds one only while the count is below 9, then shows the "Got <name>" box and
writes the save flag. Other consumables keep the vanilla rule, and the weapon and armor branches
are not touched.

The routine is byte-identical in the cart dump and in the Advance Collection's ROM, and the
patcher accepts only those two base ROMs, by MD5. The site is therefore written without reading
it first, as rom/deathlink_hook.py does.
"""
from __future__ import annotations

from typing import Dict

from .._bytemaker_compat import Entry, Patch, UInt8, count
from .address_space import gba_space

#: The consumable index this patch exempts from the cap. ram/addresses.py names it
#: ``SKULL_KEY_CONSUMABLE_INDEX``.
SKULL_KEY = 25

CAP_CHECK = Entry(0x08044316, UInt8, count(10), name="consumable cap exception test",
                  note="sub_080441F8 consumable branch, after the count > 8 test: "
                       "ldrb r1,[r4,#0xb] ; movs r0,#2 ; ands r0,r1 ; cmp r0,#0 ; bne (10 bytes)")

#: The instructions the base ROM holds at CAP_CHECK.
VANILLA = bytes.fromhex("e17a02200840002800d1")

#: cmp r2,#25 ; beq 0x08044322 ; ldrb r1,[r4,#0xb] ; lsls r1,r1,#30 ; bmi 0x08044322
PATCHED = bytes([SKULL_KEY, 0x2A]) + bytes.fromhex("03d0e17a890700d4")

assert len(VANILLA) == len(PATCHED) == 10


def build_writes() -> Dict[int, bytes]:
    """Return ``{rom_file_offset: bytes}`` that let a Skull Key pickup be collected at the cap.

    rom/patch.py applies these writes to every seed.
    """
    space = gba_space()
    patch = Patch(name="Skull Key pickup at the cap")
    CAP_CHECK.bind(space).write(PATCHED, patch=patch)
    return {edit.offset: edit.new for edit in patch.edits}
