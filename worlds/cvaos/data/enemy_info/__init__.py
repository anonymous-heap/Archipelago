"""
The game's 113 enemy types, from ``enemy_types.csv``.

Each type has the id the game uses for it, which is its row in the enemy stat table (GBA
0x080E9644) and its bit in the Enemy List's saved bitfields. The names are the game's own, read
from the enemy name text ids at GBA 0x080EA628. The drops are the item names of the type's two
drop slots, read from +0x08 and +0x0A of its stat-table row, with "" for an empty slot.
"""

from __future__ import annotations

from typing import NamedTuple

from .._csv_resources import open_csv

__all__ = [
    "EnemyType",
    "rows",
    "by_name",
    "by_id",
]


class EnemyType(NamedTuple):
    id: int
    name: str
    drops: tuple[str, str]  # slot 1, then slot 2; "" when the slot is empty


def _load() -> tuple[EnemyType, ...]:
    types = tuple(EnemyType(int(row["enemy_id"]), row["name"], (row["drop_1"], row["drop_2"]))
                  for row in open_csv(__name__, "enemy_types.csv"))
    if [enemy.id for enemy in types] != list(range(len(types))):
        raise ValueError("enemy_types.csv must list every enemy id once, in order")
    return types


rows: tuple[EnemyType, ...] = _load()
by_name: dict[str, EnemyType] = {enemy.name: enemy for enemy in rows}
by_id: dict[int, EnemyType] = {enemy.id: enemy for enemy in rows}
