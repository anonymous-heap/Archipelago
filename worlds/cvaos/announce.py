"""
Announcing a received item in game: the vanilla banner and pickup sound.

An item another world sends is granted straight into the inventory, so nothing on screen says it
arrived. The ROM-side hook (rom/received_item_box.py, installed by patch.py) shows the same
banner a floor pickup does; this module posts the request it reads.

Announcements are best-effort. They are cosmetic, so nothing here is allowed to fail or delay a
grant: every ``announce_*`` function returns quietly, and a request is skipped rather than queued
when the previous one has not been consumed yet. Missing a banner is a much smaller problem than
stalling item delivery.

Which banner:

* Items use the "Got <name>" textbox, named from the ROM's own u16 name-text-id table
  (0x08506734, indexed by item global-id) so the box reads exactly as it would for a pickup.
  ``ItemInfo.item_number`` is that global-id. With it as the index, the ROM's strings (decoded
  through the string-pointer array) match all 257 entries of ``data/item_info/item_info.csv``,
  while an off-by-one index matches none. The text-id is read from the ROM at runtime rather
  than stored here.
* A flag-only item (the Study Sealswitch) has no inventory row, so it is not in that table.
  Receiving it sets the same flag its floor copy sets. That floor copy is a custom pickup with
  its own name string in the extended text tables, so the flag is mapped back to the pickup
  through the custom-pickup registry. The announcement then uses the pickup's name and sound,
  as collecting it locally does.
* Souls use the game's own soul banner instead, which is what vanilla shows when you absorb one.
  Its ``soulIndex`` is the same within-category id ``give_item`` takes. Its ``isNew`` comes from
  the owned count after the grant, so a first soul gets vanilla's full acquisition box, which
  stops the action, and a duplicate gets the duplicate banner. With the New Soul Pause option
  off, every received soul is posted as a duplicate, so the banner is the short one that clears
  itself and the action never stops.
* Gold uses the same textbox with the "$<amount>" string a gold pickup shows, and the gold sound.
  Vanilla's gold branch takes that string from the gold pickups' table, which is read from the
  ROM at runtime too. An amount that no gold pickup holds is not announced.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .data.item_info import by_item_number as _by_item_number
from .rom import custom_pickups as _custom
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
#: ROM-domain offset of the gold pickups' table.
GOLD_TABLE_OFFSET = _box.GOLD_TABLE_GBA - GBA_ROM_BASE
ROM_DOMAIN = "ROM"

#: The soulType each soul category maps to, as SoulInventory_* and the soul banner number them.
SOUL_TYPE_BY_CATEGORY = {
    "red_soul": 0,
    "blue_soul": 1,
    "yellow_soul": 2,
    "ability_soul": 3,
}

#: The sound vanilla's collect path plays for weapons and armor. Accessories are armor ids 25 and
#: up in a pickup, so they take the armor branch. Every other item plays the plain item sound.
SFX_BY_CATEGORY = {
    "weapon": _box.SFX_ITEM_TO_INVENTORY,
    "armor": _box.SFX_ITEM_TO_INVENTORY,
    "accessory": _box.SFX_ITEM_TO_INVENTORY,
}

_text_id_cache: dict[int, int] = {}
_gold_text_ids: dict[int, int] = {}


async def _name_text_id(ram: "AoSRAM", global_id: int) -> Optional[int]:
    """The ROM's name text-id for ``global_id``, or None when the table holds 0.

    A nonzero text-id is read once and cached.
    """
    cached = _text_id_cache.get(global_id)
    if cached is not None:
        return cached
    raw = await ram.read(NAME_TABLE_OFFSET + global_id * 2, 2, ROM_DOMAIN)
    text_id = int.from_bytes(raw, "little")
    if text_id == 0:
        return None
    _text_id_cache[global_id] = text_id
    return text_id


async def _gold_text_id(ram: "AoSRAM", amount: int) -> Optional[int]:
    """The text-id of the ROM's "$<amount>" string, or None when no gold pickup holds ``amount``.

    The whole gold table is read once and cached.
    """
    if not _gold_text_ids:
        raw = await ram.read(GOLD_TABLE_OFFSET, _box.GOLD_ROW_SIZE * _box.GOLD_ROWS, ROM_DOMAIN)
        for row in range(0, len(raw), _box.GOLD_ROW_SIZE):
            row_amount = int.from_bytes(raw[row:row + 2], "little")
            text_id = int.from_bytes(raw[row + 4:row + 8], "little")
            if row_amount and text_id:
                _gold_text_ids[row_amount] = text_id
    return _gold_text_ids.get(amount)


async def _slot_is_free(ram: "AoSRAM") -> bool:
    """True when the hook has consumed the previous request."""
    pending = await ram.read(MAILBOX_OFFSET + _box.MB_PENDING, 1)
    return pending[0] == 0


async def is_ready(ram: "AoSRAM") -> bool:
    """True when a new announcement would be posted now rather than skipped.

    A caller that can choose when to grant (the Progressive Movement step) checks this first, so
    the item it grants is announced. Read errors propagate like any other RAM read.
    """
    return await _slot_is_free(ram)


def block_for(info: "ItemInfo", text_id: Optional[int], is_new: bool = True) -> Optional[bytes]:
    """The mailbox block announcing ``info``, or None for a non-soul item with no text-id."""
    soul_type = SOUL_TYPE_BY_CATEGORY.get(info.item_category)
    if soul_type is not None:
        return _box.soul_mailbox_write(soul_index=info.id, soul_type=soul_type, is_new=is_new)
    if text_id is None:
        return None
    return _box.mailbox_write(text_id, SFX_BY_CATEGORY.get(info.item_category, _box.SFX_ITEM))


async def announce_item_number(ram: "AoSRAM", item_number: int, *,
                               new_soul_pause: bool = True) -> bool:
    """Post the banner request for the item with global-id ``item_number``.

    With ``new_soul_pause`` False, every soul is announced as a duplicate, with the short banner
    and no pause.

    Returns True when a request was posted. False means it was not, for any reason: the previous
    request is still pending, the item has no banner, or a read or write failed. It is never an
    error the caller needs to handle.
    """
    try:
        info = _by_item_number.get(item_number)
        if info is None:
            return False
        if not await _slot_is_free(ram):
            return False       # the hook has not consumed the last one; skip rather than queue
        text_id = None
        is_new = False
        if info.item_category in SOUL_TYPE_BY_CATEGORY:
            # Vanilla derives isNew from the total BEFORE the add. The grant has already
            # happened here, so a count of exactly 1 means this was the first copy. With the
            # pause turned off there is no need to read the count, because every soul is posted
            # as a duplicate.
            if new_soul_pause:
                is_new = await ram.owned_count(info.item_category, info.id) == 1
        else:
            text_id = await _name_text_id(ram, item_number)
        block = block_for(info, text_id, is_new)
        if block is None:
            return False
        await ram.write(MAILBOX_OFFSET, list(block))
        return True
    except Exception:          # noqa: BLE001 - cosmetic; a grant must never fail over a banner
        from CommonClient import logger
        logger.debug("CVAoS: could not announce item %s", item_number, exc_info=True)
        return False


async def announce_gold(ram: "AoSRAM", amount: int) -> bool:
    """Post the "$<amount>" textbox and the gold sound, as picking up that much gold would.

    Returns True when a request was posted. False means it was not, for any reason: no gold
    pickup holds that amount, the previous request is still pending, or a read or write failed.
    It is never an error the caller needs to handle.
    """
    try:
        text_id = await _gold_text_id(ram, amount)
        if text_id is None or not await _slot_is_free(ram):
            return False
        await ram.write(MAILBOX_OFFSET, list(_box.mailbox_write(text_id, _box.SFX_MONEY)))
        return True
    except Exception:          # noqa: BLE001 - cosmetic; a grant must never fail over a banner
        from CommonClient import logger
        logger.debug("CVAoS: could not announce %d gold", amount, exc_info=True)
        return False


async def announce_flag(ram: "AoSRAM", flag_offset: int, flag_bit: int) -> bool:
    """Post the "Got <name>" request for a flag-only grant, when a custom pickup sets that flag.

    ``flag_offset`` and ``flag_bit`` are the EWRAM byte and bit the grant set.

    Returns True when a request was posted. False means it was not, for any reason: no custom
    pickup sets that flag, the previous request is still pending, or a read or write failed. It
    is never an error the caller needs to handle.
    """
    try:
        found = _custom.announcement_for_flag(flag_offset, flag_bit)
        if found is None:
            return False
        text_id, sfx = found
        if text_id == 0 or not await _slot_is_free(ram):
            return False
        await ram.write(MAILBOX_OFFSET, list(_box.mailbox_write(text_id, sfx or _box.SFX_ITEM)))
        return True
    except Exception:          # noqa: BLE001 - cosmetic; a grant must never fail over a banner
        from CommonClient import logger
        logger.debug("CVAoS: could not announce flag %#x bit %d", flag_offset, flag_bit,
                     exc_info=True)
        return False
