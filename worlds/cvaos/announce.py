"""
Announcing a received item in game: the vanilla banner and pickup sound.

An item another world sends is granted straight into the inventory, so nothing on screen says it
arrived. The ROM-side hook (rom/received_item_box.py, installed by patch.py) shows the same
banner a floor pickup does; this module posts the request it reads.

Deliberately best-effort. An announcement is cosmetic, so nothing here is allowed to fail or
delay a grant: every path returns quietly, and a request is skipped rather than queued when the
previous one has not been consumed yet. Missing a banner is a much smaller problem than stalling
item delivery.

Which banner:

* Items use the "Got <name>" textbox, named from the ROM's own u16 name-text-id table
  (0x08506734, indexed by item global-id) so the box reads exactly as it would for a pickup.
  ``ItemInfo.item_number`` IS that global-id: all 257 entries were verified by decoding the
  ROM's own strings through the string-pointer array and matching them against
  ``data/item_info/item_info.csv`` (every one matched, and the off-by-one alternative matched
  none). The text-id is read from the ROM at runtime rather than baked in here.
* Souls use the game's own soul banner instead, which is what vanilla shows when you absorb one.
  Its ``soulIndex`` is the same within-category id ``give_item`` takes.
* Money is not announced: vanilla routes gold through a different banner, and the gold counter
  updating on screen already shows it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .data.item_info import by_item_number as _by_item_number
from .rom import received_item_box as _box

if TYPE_CHECKING:
    from .data.item_info import ItemInfo
    from .ram import AoSRAM

EWRAM_BASE = 0x02000000
GBA_ROM_BASE = 0x08000000

#: EWRAM-domain offset of the hook's mailbox.
MAILBOX_OFFSET = _box.MAILBOX_GBA - EWRAM_BASE
#: ROM-domain offset of the u16 item-name text-id table.
NAME_TABLE_OFFSET = _box.ITEM_NAME_TEXT_IDS_GBA - GBA_ROM_BASE
ROM_DOMAIN = "ROM"

#: The soulType each soul category maps to, as SoulInventory_* and the soul banner number them.
SOUL_TYPE_BY_CATEGORY = {
    "red_soul": 0,
    "blue_soul": 1,
    "yellow_soul": 2,
    "ability_soul": 3,
}

_text_id_cache: dict[int, int] = {}


async def _name_text_id(ram: "AoSRAM", global_id: int) -> Optional[int]:
    """The ROM's name text-id for ``global_id``, read once and cached."""
    cached = _text_id_cache.get(global_id)
    if cached is not None:
        return cached
    raw = await ram.read(NAME_TABLE_OFFSET + global_id * 2, 2, ROM_DOMAIN)
    text_id = int.from_bytes(raw, "little")
    if text_id == 0:
        return None
    _text_id_cache[global_id] = text_id
    return text_id


async def _slot_is_free(ram: "AoSRAM") -> bool:
    """True when the hook has consumed the previous request."""
    pending = await ram.read(MAILBOX_OFFSET + _box.MB_PENDING, 1)
    return pending[0] == 0


def block_for(info: "ItemInfo", text_id: Optional[int]) -> Optional[bytes]:
    """The mailbox block announcing ``info``, or None when it should not be announced."""
    soul_type = SOUL_TYPE_BY_CATEGORY.get(info.item_category)
    if soul_type is not None:
        return _box.soul_mailbox_write(soul_index=info.id, soul_type=soul_type)
    if text_id is None:
        return None
    return _box.mailbox_write(text_id)


async def announce_item_number(ram: "AoSRAM", item_number: int) -> bool:
    """Post the banner request for the item with global-id ``item_number``.

    Returns True when a request was posted. False means "not announced" for any reason -- the
    previous request is still pending, the item has no banner, or a read/write failed -- and is
    never an error the caller needs to handle.
    """
    try:
        info = _by_item_number.get(item_number)
        if info is None:
            return False
        if not await _slot_is_free(ram):
            return False       # the hook has not consumed the last one; skip rather than queue
        text_id = None
        if info.item_category not in SOUL_TYPE_BY_CATEGORY:
            text_id = await _name_text_id(ram, item_number)
        block = block_for(info, text_id)
        if block is None:
            return False
        await ram.write(MAILBOX_OFFSET, list(block))
        return True
    except Exception:          # noqa: BLE001 - cosmetic; a grant must never fail over a banner
        from CommonClient import logger
        logger.debug("CVAoS: could not announce item %s", item_number, exc_info=True)
        return False
