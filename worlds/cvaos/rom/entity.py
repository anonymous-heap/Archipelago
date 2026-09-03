"""
AoS ROM entity record, declared once as a bytemaker ``Struct``.
"""
from __future__ import annotations

from .._bytemaker_compat import Struct, s16, sizeof, u8, u16

GBA_ROM_BASE = 0x08000000


class AoSPickupEntity(Struct, endian="little"):
    """
    12-byte AoS room entity as stored in ROM (the pickup record; room object lists use the
    same layout with ``type``/``subtype`` meaning object kind and ``var_a``/``var_b`` object
    parameters).

    Offset  Size  Field
    0x00    2     x           Position
    0x02    2     y           Position
    0x04    1     entity_id   Per-room unique ID
    0x05    1     type        Entity type (4=PICKUP, 5=HARD_PICKUP)
    0x06    1     subtype     Item category (1=money, 2=consumable, 3=weapon,
                               4=armor, 6=ability soul, 7=guardian soul, 8=enchant soul)
    0x07    1     unknown     Always 0x00
    0x08    2     var_a       flag_offset — per-location save flag. Unchanged when shuffling items.
    0x0A    2     var_b       item_offset — index of item within its subtype category

    Fields read as plain ``int``; stores narrow to the field width. ``pack()`` / ``parse()``
    are the byte conversions; ``offset_of(AoSPickupEntity, name)`` answers where a field sits.
    """

    x: s16
    y: s16
    entity_id: u8
    type: u8
    subtype: u8
    unknown: u8
    var_a: u16
    var_b: u16


ENTITY_SIZE = sizeof(AoSPickupEntity)
