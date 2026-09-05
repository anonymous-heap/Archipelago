"""
Received-item announcement: the vanilla "Got <name>" textbox + pickup SFX for an item another
world sent us.

The ROM owns the whole sequence. The client only posts a request into a mailbox in free EWRAM;
a per-frame hook reads it, waits for the game to be able to take a textbox, then makes the two
calls vanilla makes on a normal pickup. Doing it ROM-side is what makes the announcement
reliable: the game's textbox request has a busy state, and a client writing the request fields
directly would have its box silently dropped whenever a transition or another box was up.

* ``sub_0800EF98`` (GBA 0x0800EF98) queues the "Got <name>" box. It is two stores behind a
  guard -- ``if (!(gEwramData->unk_60.unk_42C & 0x03000200)) { unk_423 = 1; unk_420 = text_id; }``
  at absolute 0x0200042C (the ewram.h field offsets are absolute from gEwramData, NOT relative
  to unk_60; reading them relative was a real bug, caught in a playtest by the box being
  dropped mid-cutscene while the sound still played)
  -- and ``sub_0800EFD4`` dispatches request kind 1 to the real textbox ``sub_0800E0E8`` on a
  later frame. The hook checks that same guard so a request is retried rather than lost.
* ``PlaySong`` (GBA 0x080D7910) plays the sound. Vanilla pickup ids: 0xB4 money, 0xB5 item,
  0xB6 soul, 0xB7 item-to-inventory. :data:`SFX_ITEM` is the plain item sound.
* Item name text-ids are the u16 table at 0x08506734 indexed by item global-id, which is the
  same table the vanilla collect path (``sub_080441F8``) reads. :func:`name_text_id` wraps it.
* Souls use a different renderer, not the textbox. The hook makes the three calls vanilla
  makes, in vanilla's order: ``sub_08045C34(soulType)`` for a new soul only (its effect entity
  freezes the entity update loop, which is the pause the player dismisses with A),
  ``sub_0800E708(soulIndex, soulType, isNew)`` to start the banner, and
  ``sub_08049E64(soulType, soulIndex)`` to queue the soul the banner displays. Then
  ``PlaySong(0xBC)``. Mind the argument order: it is reversed between those last two, and
  reversed again relative to the ``SoulInventory_*`` functions. :func:`soul_mailbox_write` takes
  the SoulInventory order and the hook sorts it out.

Source: received_item_box.s, beside this file, which rom/thumb_assembler.py assembles; the blob
below is exactly what it produces, and test_thumb_assembler.py holds the two together. The pool
holds no address of its own, so the bytes are the same at any word-aligned link address (see the
relocation note in the .s).

The per-frame hook is registered in the shared update-hook framework
(rom/xanthus_framework.py), which owns the 0x08043104 pointer and the dispatcher. This feature
owns slot 2 and is independent of every other slot's owner, in either apply order.
"""
from __future__ import annotations

from typing import Dict

from . import xanthus_framework

GBA_ROM_BASE = 0x08000000

# --- This hook: slot 2 of the shared framework's pointer list ---
HOOK_SLOT = 2
HOOK_BODY_GBA = xanthus_framework.slot_body_gba(HOOK_SLOT)

# Assembled from received_item_box.s at -Ttext=0x087D0300 (THUMB, ARMv4T). 104 bytes of code
# plus an 8-word literal pool: the mailbox, the textbox state word, the busy mask, and
# sub_0800EF98|1 / sub_08045C34|1 / sub_0800E708|1 / sub_08049E64|1 / PlaySong|1.
_HOOK_BODY = bytes.fromhex(
    "ffb400b5184ce079002828d0174800681749084223d12079002806d120880028"
    "15d0144b00f01ff811e0a079002803d06079114b00f017f820886179a2790f4b"
    "00f011f8607921880d4b00f00cf86088002802d00b4b00f006f80020e07102bc"
    "8e46ffbc7047184700f003022c0400020002000399ef0008355c040809e70008"
    "659e040811790d08"
)

# --- Mailbox: free high EWRAM, transient (NOT SRAM-saved) ---
# 0x02025554..0x02040000 is zero-filled once at boot and never touched by game code. This is
# clear of the Item-Use shadow array (0x02030000..0x0203002B) and of classicvania_movement's
# scratch (0x0203E000).
MAILBOX_GBA = 0x0203F000
MB_ARG0 = 0       # u16: item -> name text-id (0 = sound only); soul -> soulIndex
MB_SFX = 2        # u16: PlaySong id; 0 = silent
MB_KIND = 4       # u8:  KIND_ITEM or KIND_SOUL
MB_ARG1 = 5       # u8:  soul -> soulType   (unused for items)
MB_ARG2 = 6       # u8:  soul -> isNew      (unused for items)
MB_PENDING = 7    # u8:  client writes 1 to request; the hook writes 0 once it has fired
MAILBOX_SIZE = 8

KIND_ITEM = 0     # sub_0800EF98(text_id): the "Got <name>" textbox
KIND_SOUL = 1     # sub_0800E708(soulIndex, soulType, isNew): the soul banner

# Vanilla acquisition sounds (PlaySong ids), each read off its branch of sub_080441F8.
SFX_CATEGORY_0 = 0xB4          # category 0 (sub_08043BB0)
SFX_ITEM = 0xB5                # categories 2, 3
SFX_MONEY = 0xB6               # category 1, the gold branch (caps at 999999)
SFX_ITEM_TO_INVENTORY = 0xB7   # categories 5..8, via sub_0804AE3C
SFX_SOUL = 0xBC                # the soul branch

#: u16 table of item NAME text-ids, indexed by item global-id (257 entries).
ITEM_NAME_TEXT_IDS_GBA = 0x08506734

assert len(_HOOK_BODY) == 136, f"hook body must be 136 bytes, got {len(_HOOK_BODY)}"
assert len(_HOOK_BODY) <= 0x200, "hook body overruns its 0x200-byte slot"
assert MAILBOX_GBA.to_bytes(4, "little") in _HOOK_BODY, "mailbox addr missing from hook body"
assert (0x0800EF98 + 1).to_bytes(4, "little") in _HOOK_BODY, "sub_0800EF98 missing from hook body"
assert (0x0200042C).to_bytes(4, "little") in _HOOK_BODY, "busy-guard addr missing from hook body"
assert (0x0800E708 + 1).to_bytes(4, "little") in _HOOK_BODY, "sub_0800E708 missing from hook body"
assert (0x08049E64 + 1).to_bytes(4, "little") in _HOOK_BODY, "sub_08049E64 missing from hook body"
assert (0x08045C34 + 1).to_bytes(4, "little") in _HOOK_BODY, "sub_08045C34 missing from hook body"
assert (0x080D7910 + 1).to_bytes(4, "little") in _HOOK_BODY, "PlaySong missing from hook body"


def name_text_id(base_rom: bytes, item_global_id: int) -> int:
    """The "Got <name>" string id for ``item_global_id``, read from the ROM's own table.

    This is the table the vanilla collect path reads, so an announcement names an item exactly
    as picking it up would.
    """
    if not 0 <= item_global_id <= 256:
        raise ValueError(f"item global-id out of range: {item_global_id}")
    at = ITEM_NAME_TEXT_IDS_GBA - GBA_ROM_BASE + item_global_id * 2
    return int.from_bytes(base_rom[at:at + 2], "little")


def mailbox_write(text_id: int, sfx: int = SFX_ITEM) -> bytes:
    """The 8 mailbox bytes announcing one item, for the client to write at :data:`MAILBOX_GBA`.

    Write the block in one transfer: ``pending`` is its final byte, so the request cannot be
    seen half-filled. Do not post another request while ``pending`` still reads 1 -- the ROM
    clears it once the banner is queued, which also paces announcements so they cannot stomp
    each other. Queue the rest client-side and post the next when it clears.
    """
    if not 0 <= text_id <= 0xFFFF:
        raise ValueError(f"text_id out of range: {text_id}")
    if not 0 <= sfx <= 0xFFFF:
        raise ValueError(f"sfx out of range: {sfx}")
    return (text_id.to_bytes(2, "little") + sfx.to_bytes(2, "little")
            + bytes([KIND_ITEM, 0, 0, 1]))


def soul_mailbox_write(soul_index: int, soul_type: int, is_new: bool = True,
                       sfx: int = SFX_SOUL) -> bytes:
    """The 8 mailbox bytes announcing one soul, for the client to write at :data:`MAILBOX_GBA`.

    ``soul_type``/``soul_index`` are the pair the ``SoulInventory_*`` functions take, in that
    order -- note the ROM's banner call ``sub_0800E708`` takes them the other way round, which
    the hook handles. ``is_new`` is what vanilla derives from ``SoulInventory_GetSoulTotal``.

    This announces only; it does not add the soul to the inventory.
    """
    if not 0 <= soul_index <= 0xFFFF:
        raise ValueError(f"soul_index out of range: {soul_index}")
    if not 0 <= soul_type <= 0xFF:
        raise ValueError(f"soul_type out of range: {soul_type}")
    if not 0 <= sfx <= 0xFFFF:
        raise ValueError(f"sfx out of range: {sfx}")
    return (soul_index.to_bytes(2, "little") + sfx.to_bytes(2, "little")
            + bytes([KIND_SOUL, soul_type, 1 if is_new else 0, 1]))


def build_writes(base_rom: bytes) -> Dict[int, bytes]:
    """``{rom_file_offset: bytes}`` installing the announcement hook in slot 2 of the shared
    update-hook framework (rom/xanthus_framework.py, which validates the ROM state)."""
    return xanthus_framework.writes(base_rom, HOOK_SLOT, _HOOK_BODY)
