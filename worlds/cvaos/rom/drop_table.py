"""
Write a seed's drop table into the enemy stat table.

``rom_config.json`` holds the table under ``drop_table``, as four numbers for each enemy row, keyed
by the enemy id as a string. They are the common drop's stored value and rarity byte, then the
rare drop's, which are EnemyDNA's ``drop_1``, ``drop_1_rarity``, ``drop_2`` and ``drop_2_rarity``
(``rom/enemy_table.py``). A stored value is the item's number plus 1, or 0 for an empty slot.

The spawn routine (``sub_08044054``) subtracts the 1 and reads the result by ranges: up to 0x1F a
consumable, up to 0x5A a weapon, and up to 0x87 an armor or an accessory. Above that, it reads an
item record at address 0. An off-by-one therefore does more than rename an item. A missing 1 turns
Casual Clothes into Valmanway and empties a Potion slot, and an extra 1 turns Gold Ring into Chaos
Ring. So :func:`encode_table` and :func:`build_writes` both refuse a filled slot whose stored value
is outside 1 to MAX_VALUE.

The patch writes the table before Enemy Drop Rate scales each common rarity
(``rom/enemy_drop_rates.py``), so that option scales the byte that each slot's new item brings.
"""
from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .._bytemaker_compat import Patch
from ..data import enemy_type_collection
from ..data.item_info import by_name as item_info_by_name
from .address_space import gba_space
from .enemy_table import ENEMY_COUNT, ENEMY_TABLE, row_offset

__all__ = ["FIELDS", "MAX_VALUE", "build_writes", "encode_table", "stored_value"]

# The row fields that each listed row sets, in the order rom_config.json lists them.
FIELDS = ("drop_1", "drop_1_rarity", "drop_2", "drop_2_rarity")
_ITEM_FIELDS = frozenset({"drop_1", "drop_2"})

# The largest value the spawn routine decodes: the last accessory's number (0x87) plus 1.
MAX_VALUE = 0x88


def _check_value(value: int, where: str) -> None:
    if not 1 <= value <= MAX_VALUE:
        raise ValueError(f"{where}: a drop slot can't store {value:#x}, because the game reads "
                         f"only 1 to {MAX_VALUE:#x} as an item")


def stored_value(name: Optional[str]) -> int:
    """Return the value that a drop slot stores for the item named ``name``, or 0 for None.

    Raises ValueError for an item that no drop slot can hold, such as a soul.
    """
    if name is None:
        return 0
    value = item_info_by_name[name].item_number + 1
    _check_value(value, name)
    return value


def encode_table(table: Mapping[int, Sequence]) -> Dict[str, List[int]]:
    """Return ``table`` in the form that ``rom_config.json`` stores under ``drop_table``.

    ``table`` maps each enemy id to its two slots, common first. Each slot has an ``item`` (a name,
    or None when the slot is empty) and a ``rarity`` byte, as ``drop_shuffle.DropSlot`` does.
    """
    rows: Dict[str, List[int]] = {}
    for enemy_id, slots in sorted(table.items()):
        if len(slots) != 2:
            raise ValueError(f"enemy {enemy_id} has {len(slots)} drop slots, not 2")
        rows[str(enemy_id)] = [number for slot in slots
                               for number in (stored_value(slot.item), slot.rarity)]
    return rows


def _vanilla_row(enemy_id: int) -> Tuple[int, ...]:
    enemy = enemy_type_collection[enemy_id]
    return tuple(number for item, rarity in zip(enemy.drops, enemy.drop_rarities)
                 for number in (stored_value(item or None), rarity))


def build_writes(rom: bytes, rows: Mapping[str, Sequence[int]]) -> Dict[int, bytes]:
    """
    Return the writes that set each listed row's two drop slots.

    Args:
        rom: a ROM whose listed rows still hold the game's own drops, which this checks first.
        rows: ``{enemy id: [drop_1, drop_1_rarity, drop_2, drop_2_rarity]}``, with each id as a
            string, as rom_config.json stores it. A row that is not listed keeps its bytes.

    Returns ``{file offset: bytes}`` for the fields that change. Raises ValueError for an enemy id
    outside the table, a filled slot outside 1 to MAX_VALUE, a rarity that is not a byte, or a row
    that no longer holds its vanilla drops.
    """
    enemies = ENEMY_TABLE.bind(gba_space(rom))
    patch = Patch(name="drop table")
    for key, values in rows.items():
        enemy_id = int(key)
        if not 0 <= enemy_id < ENEMY_COUNT:
            raise ValueError(f"drop_table names enemy {enemy_id}, which the table lacks")
        row = enemies.item(enemy_id)
        old = tuple(row.field(name).read() for name in FIELDS)
        if old != _vanilla_row(enemy_id):
            raise ValueError(f"enemy {enemy_id}'s drop fields at {row_offset(enemy_id):#x} hold "
                             f"{old}, expected {_vanilla_row(enemy_id)} (ROM mismatch)")
        for name, value, current in zip(FIELDS, values, old, strict=True):
            if name in _ITEM_FIELDS and value:
                _check_value(value, f"enemy {enemy_id}'s {name}")
            elif name not in _ITEM_FIELDS and not 0 <= value <= 0xFF:
                raise ValueError(f"enemy {enemy_id}'s {name} of {value} is not a byte")
            if value != current:
                row.field(name).write(value, expect=current, patch=patch)
    return {edit.offset: edit.new for edit in patch.edits}
