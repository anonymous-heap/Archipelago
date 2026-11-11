"""
Encoding and granting received items for the Aria of Sorrow clients.

Every cvaos AP item code is a packed integer. ``pack`` builds one at generation time
(worlds/cvaos/items.py); ``resolve`` unpacks a received code into a ``ReceiveAction``, and
``grant`` performs it through the RAM accessors. This module defines the encoding and applies
received items, with one exception. A Progressive Movement copy changes nothing here, because
the client grants the stage souls by counting the copies (client_logic).

Bit layout (MSB->LSB, 53 bits, because AP requires ``0 < id < 2**53`` for JS clients):

    [52:49] category   (4)  TransferCategory; 0 reserved so every code is > 0
    [48]    set_flag   (1)  also set an EWRAM flag bit?
    [47:40] unused     (8)  reserved for growth
    [39:28] id/value  (12)  item -> item_info.item_number; money -> gold value
    [27:22] retired    (6)  always 0 in new codes. Codes from older seeds use it to tell
                            copies of one item apart, so it is never reused (canonical_code)
    [21:4]  flag off  (18)  EWRAM byte offset (when set_flag)
    [3:1]   flag bit   (3)  bit index 0-7 (when set_flag)
    [0]     flag value (1)  value to set that bit to (when set_flag)

``other_item`` is granted the same way as ``pickup``. ``event`` and ``to_be_implemented`` are
reserved, so ``grant`` logs and skips them. A Castle Map is a plain item transfer, because the
game reveals a map's areas while Soma holds it.
"""
from __future__ import annotations

from enum import IntEnum
from typing import NamedTuple, Sequence, TYPE_CHECKING

from .data.item_info import by_item_number as _by_item_number

if TYPE_CHECKING:
    from .ram import AoSRAM
    from .ram.backend import GuardedWrite


class TransferCategory(IntEnum):
    # 0 is reserved (unused) so that category, sitting in the high bits, keeps every code > 0.
    PICKUP = 1             # id = item_info.item_number -> give_item
    OTHER_ITEM = 2         # id = item_info.item_number -> give_item (item with no ground pickup)
    MONEY = 3              # id = gold value -> add_gold
    EVENT = 4              # reserved
    TO_BE_IMPLEMENTED = 5  # reserved
    FLAG_ONLY = 6          # set_flag is the entire grant (no item/gold transfer)
    PROGRESSIVE = 7        # id unused; the client grants stage souls by counting copies


# --- bit layout (shifts + masks) ---
_CATEGORY_SHIFT, _CATEGORY_MASK = 49, 0xF
_SET_FLAG_SHIFT = 48
_ID_SHIFT, _ID_MASK = 28, 0xFFF            # 12 bits
_RETIRED_SHIFT, _RETIRED_MASK = 22, 0x3F  # 6 bits, always 0 in new codes
_FLAG_OFFSET_SHIFT, _FLAG_OFFSET_MASK = 4, 0x3FFFF  # 18 bits (256 KB EWRAM)
_FLAG_BIT_SHIFT, _FLAG_BIT_MASK = 1, 0x7    # 3 bits
_FLAG_VALUE_MASK = 0x1

ID_MAX = _ID_MASK            # 4095: largest packable item_number / gold value


def pack(category: TransferCategory, id_or_value: int, *,
         set_flag: bool = False, flag_offset: int = 0, flag_bit: int = 0, flag_value: int = 0) -> int:
    """Build a packed AP item code, raising if any field overflows its width."""
    if not 0 <= id_or_value <= _ID_MASK:
        raise ValueError(f"id/value {id_or_value} does not fit in 12 bits")
    if not 0 <= flag_offset <= _FLAG_OFFSET_MASK:
        raise ValueError(f"flag_offset {flag_offset:#x} does not fit in 18 bits")
    if not 0 <= flag_bit <= _FLAG_BIT_MASK:
        raise ValueError(f"flag_bit {flag_bit} does not fit in 3 bits")
    return (
        (int(category) << _CATEGORY_SHIFT)
        | (int(bool(set_flag)) << _SET_FLAG_SHIFT)
        | (id_or_value << _ID_SHIFT)
        | (flag_offset << _FLAG_OFFSET_SHIFT)
        | (flag_bit << _FLAG_BIT_SHIFT)
        | (flag_value & _FLAG_VALUE_MASK)
    )


class ReceiveAction(NamedTuple):
    category: TransferCategory
    id_or_value: int    # item -> item_info.item_number; money -> gold value
    set_flag: bool
    flag_offset: int
    flag_bit: int
    flag_value: int


def canonical_code(code: int) -> int:
    """Return ``code`` with its retired field cleared.

    A seed from an older version can send a code that sets the retired field. Clearing it gives the
    code that the item has now.
    """
    return code & ~(_RETIRED_MASK << _RETIRED_SHIFT)


def resolve(code: int) -> "ReceiveAction | None":
    """Unpack a received AP code into a ``ReceiveAction``.

    Returns ``None`` for an undefined category, which the caller logs and skips. The retired field
    is ignored, so a code from an older seed grants the same item.
    """
    try:
        category = TransferCategory((code >> _CATEGORY_SHIFT) & _CATEGORY_MASK)
    except ValueError:
        return None
    return ReceiveAction(
        category=category,
        id_or_value=(code >> _ID_SHIFT) & _ID_MASK,
        set_flag=bool((code >> _SET_FLAG_SHIFT) & 1),
        flag_offset=(code >> _FLAG_OFFSET_SHIFT) & _FLAG_OFFSET_MASK,
        flag_bit=(code >> _FLAG_BIT_SHIFT) & _FLAG_BIT_MASK,
        flag_value=code & _FLAG_VALUE_MASK,
    )


async def grant(ram: "AoSRAM", action: ReceiveAction, *, new_soul_pause: bool = True,
                together_with: Sequence["GuardedWrite"] = ()) -> bool:
    """Apply a resolved action through the RAM accessors.

    Returns ``False`` only if a guarded write lost a race, and the caller then retries next tick.

    ``together_with`` holds other guarded writes to make in the same step as the transfer, so
    either all of them happen or none do. The client passes the received-counter's advance here,
    so a ``False`` return also means the counter was not advanced. When an action has no transfer,
    ``grant`` makes these writes as a batch of their own.

    ``new_soul_pause`` is the New Soul Pause option: whether a soul the player did not already
    own is announced with the game's stop-the-action first-soul box, or with the short banner a
    duplicate gets. It only shapes the announcement, never the grant.
    """
    # ORDER MATTERS: set the flag FIRST, and make the transfer LAST, in the same batch as the
    # received-counter's advance. Nothing after that batch is retried, so the flag has to be set
    # before it. If the flag write loses its guarded race, we return before any transfer. If the
    # transfer loses, setting the flag again next tick is harmless, because setting a bit that
    # already has the target value changes nothing. Do not reorder.
    if action.set_flag:
        if not await ram.set_flag_bit(action.flag_offset, action.flag_bit, action.flag_value):
            return False
    return await _grant_transfer(ram, action, new_soul_pause, together_with)


async def _grant_transfer(ram: "AoSRAM", action: ReceiveAction, new_soul_pause: bool,
                          together_with: Sequence["GuardedWrite"]) -> bool:
    category = action.category
    if category == TransferCategory.MONEY:
        granted = await ram.add_gold(action.id_or_value, together_with=together_with)
        if granted:
            from . import announce
            await announce.announce_gold(ram, action.id_or_value)
        return granted
    if category in (TransferCategory.PICKUP, TransferCategory.OTHER_ITEM):
        info = _by_item_number[action.id_or_value]
        granted = await ram.give_item(info.item_category, info.id, together_with=together_with)
        if granted:
            # The announcement is cosmetic, and runs only once the item is in the inventory.
            # announce_* never raises and never waits for the mailbox, so it cannot turn a
            # delivered item into a retry.
            from . import announce
            await announce.announce_item_number(ram, action.id_or_value,
                                                new_soul_pause=new_soul_pause)
        return granted
    if category == TransferCategory.PROGRESSIVE:
        # A progressive copy grants nothing here, so only the together_with writes remain. The
        # client counts every copy found or received and grants the stage souls for that count
        # (client_logic). Counting also restores those souls after an older save is reloaded.
        return await ram.guarded_write_many(together_with)
    if category == TransferCategory.FLAG_ONLY:
        # The whole grant was the set_flag bit (already applied in grant()), so only the
        # together_with writes remain, and they go before the announcement. If a custom pickup
        # sets that same flag on collection (the Study Sealswitch), announce it under the pickup's
        # name, as the floor copy does; the id field carries nothing to name it by.
        if not await ram.guarded_write_many(together_with):
            return False
        from . import announce
        await announce.announce_flag(ram, action.flag_offset, action.flag_bit)
        return True
    # EVENT and TO_BE_IMPLEMENTED are reserved and grant nothing. Log a warning and skip the item,
    # but still make the together_with writes so the received-counter moves past it.
    from CommonClient import logger
    logger.warning("CVAoS: received reserved category %s; skipping.", category.name)
    return await ram.guarded_write_many(together_with)
