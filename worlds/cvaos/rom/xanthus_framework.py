"""
The Xanthus update-hook framework: shared infrastructure for per-frame ROM hooks.

Ported from Xanthus's aos_patches (https://github.com/Xanthus1/aos_patches) with their generous
permission, as part of the Classicvania Movement patch. It is kept here rather than inside any
one feature because the GBA offers exactly one seat for it, so every per-frame hook must share
this one installation:

* The USA ROM keeps a per-frame function pointer -- vanilla ``0x0804306D``, the HP-display
  update -- at GBA ``0x08043104``.
* :data:`DISPATCHER` is repointed there. It calls the original function, then walks a
  :data:`SLOT_COUNT`-entry pointer list at ``0x087D0040`` and calls every non-zero entry.
* Slot bodies live at ``0x087D0100 + 0x200*(slot-1)``, so each slot has 0x200 bytes.

A hook body must preserve r0-r7 and lr: the dispatcher's loop keeps its list pointer and offset
live in r0/r1 across the call. The convention both current bodies use is::

    push {r0-r7}
    push {lr}
    ...
    pop {r1}
    mov lr, r1
    pop {r0-r7}
    bx  lr

:func:`writes` installs the framework and registers one slot. The framework half is idempotent
and tolerant: patch.py hands each module the ROM as patched so far, so a second feature finds
the dispatcher already in place and simply emits the same bytes. That makes the features
independent of each other and of their apply order. The slot half is strict, so two features
claiming one slot, or a body landing on an occupied region, fails loudly.

Slot allocations (keep this list current; a slot may have exactly one owner):

===== ======================= ==============================================
Slot  Owner                   Body
===== ======================= ==============================================
1     classicvania_movement   no-air-control hook
2     received_item_box       received item/soul announcement
3-12  free
===== ======================= ==============================================
"""
from __future__ import annotations

from typing import Dict

GBA_ROM_BASE = 0x08000000

#: The per-frame function pointer this framework takes over.
HOOK_SITE_OFFSET = 0x043104
#: Low 3 bytes of the vanilla pointer (0x0804306D); the high 0x08 byte is reused.
HOOK_SITE_VANILLA = bytes.fromhex("6d3004")
#: Low 3 bytes completing the word to 0x087D0001 (the dispatcher | thumb).
HOOK_SITE_PATCHED = bytes.fromhex("01007d")

DISPATCHER_OFFSET = 0x7D0000
#: Calls vanilla 0x0804306D, then every non-zero entry of the slot list. 52 bytes including its
#: literal pool (0x0804306D, 0x087D0040) and the two `bx` veneers.
DISPATCHER = bytes.fromhex(
    "07b400b5084900f013f808480021302906d04258002a01d000f00bf80431f6e7"
    "01bc864607bc70476d30040840007d0808471047"
)

HOOK_LIST_OFFSET = 0x7D0040
SLOT_COUNT = 12
SLOT_ENTRY_SIZE = 4
SLOT_BODY_BASE_GBA = 0x087D0100
SLOT_BODY_STRIDE = 0x200

assert len(DISPATCHER) == 52, f"dispatcher must be 52 bytes, got {len(DISPATCHER)}"


def slot_body_gba(slot: int) -> int:
    """GBA address of ``slot``'s hook body."""
    _check_slot(slot)
    return SLOT_BODY_BASE_GBA + SLOT_BODY_STRIDE * (slot - 1)


def slot_entry_offset(slot: int) -> int:
    """ROM file offset of ``slot``'s entry in the dispatcher's pointer list."""
    _check_slot(slot)
    return HOOK_LIST_OFFSET + SLOT_ENTRY_SIZE * (slot - 1)


def _check_slot(slot: int) -> None:
    if not 1 <= slot <= SLOT_COUNT:
        raise ValueError(f"slot must be 1..{SLOT_COUNT}, got {slot}")


def writes(base_rom: bytes, slot: int, body: bytes) -> Dict[int, bytes]:
    """``{rom_file_offset: bytes}`` installing the framework and registering ``body`` in ``slot``.

    ``base_rom`` is the ROM as patched so far. The framework may already be installed by another
    feature, which is fine and emits the same bytes; anything else at the hook site or the
    dispatcher region raises. ``slot`` must be unclaimed and its body region empty.
    """
    _check_slot(slot)
    if len(body) > SLOT_BODY_STRIDE:
        raise ValueError(
            f"slot {slot} body is {len(body)} bytes, over the {SLOT_BODY_STRIDE}-byte slot"
        )

    site = base_rom[HOOK_SITE_OFFSET:HOOK_SITE_OFFSET + len(HOOK_SITE_VANILLA)]
    if site not in (HOOK_SITE_VANILLA, HOOK_SITE_PATCHED):
        raise ValueError(
            f"xanthus_framework: hook-site bytes at {HOOK_SITE_OFFSET:#x} are {site.hex()}, "
            f"expected vanilla {HOOK_SITE_VANILLA.hex()} or this dispatcher "
            f"{HOOK_SITE_PATCHED.hex()} (ROM mismatch, or a third party took the pointer)"
        )

    installed = base_rom[DISPATCHER_OFFSET:DISPATCHER_OFFSET + len(DISPATCHER)]
    if any(installed) and installed != DISPATCHER:
        raise ValueError(
            f"xanthus_framework: {DISPATCHER_OFFSET:#x} holds something other than the "
            f"dispatcher; free space collision"
        )

    entry_at = slot_entry_offset(slot)
    if any(base_rom[entry_at:entry_at + SLOT_ENTRY_SIZE]):
        raise ValueError(f"xanthus_framework: slot {slot} is already claimed")

    body_at = slot_body_gba(slot) - GBA_ROM_BASE
    if any(base_rom[body_at:body_at + len(body)]):
        raise ValueError(
            f"xanthus_framework: slot {slot} body region at {body_at:#x} (+{len(body):#x}) is "
            f"not empty; free space collision"
        )

    return {
        HOOK_SITE_OFFSET: HOOK_SITE_PATCHED,
        DISPATCHER_OFFSET: DISPATCHER,
        entry_at: (slot_body_gba(slot) + 1).to_bytes(4, "little"),   # | thumb
        body_at: body,
    }
