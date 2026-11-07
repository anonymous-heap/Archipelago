"""
The game's 113 enemy types, from ``enemy_types.csv`` and ``enemy_stats.csv``.

Each type has the id the game uses for it, which is its row in the enemy stat table (GBA
0x080E9644) and its bit in the Enemy List's saved bitfields. The names are the game's own, read
from the enemy name text ids at GBA 0x080EA628. The drops are the item names of the type's two
drop slots, read from +0x08 and +0x0A of its stat-table row, with "" for an empty slot. Each field
holds the item's index plus 1, and 0 for an empty slot. The game subtracts the 1 when it spawns the
drop (sub_08044054 at 0x080440E6).

``enemy_stats.csv`` adds each type's HP (+0x0C) and the rarity byte of each drop slot (+0x15 for
slot 1, +0x16 for slot 2). ``build_enemy_stats.py`` reads them from the ROM and says how the game's
code uses each one.
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
    hp: int
    drop_rarities: tuple[int, int]  # slot 1, then slot 2; a higher byte drops less often


def _load() -> tuple[EnemyType, ...]:
    stats = {int(row["enemy_id"]): row for row in open_csv(__name__, "enemy_stats.csv")}
    types = []
    for row in open_csv(__name__, "enemy_types.csv"):
        enemy_id = int(row["enemy_id"])
        stat = stats.get(enemy_id)
        if stat is None or stat["name"] != row["name"]:
            raise ValueError(f"enemy_stats.csv has no row for enemy {enemy_id} ({row['name']})")
        types.append(EnemyType(enemy_id, row["name"], (row["drop_1"], row["drop_2"]),
                               int(stat["hp"]),
                               (int(stat["drop_1_rarity"]), int(stat["drop_2_rarity"]))))
    if [enemy.id for enemy in types] != list(range(len(types))):
        raise ValueError("enemy_types.csv must list every enemy id once, in order")
    if len(stats) != len(types):
        raise ValueError("enemy_stats.csv must list the same enemy types as enemy_types.csv")
    return tuple(types)


rows: tuple[EnemyType, ...] = _load()
by_name: dict[str, EnemyType] = {enemy.name: enemy for enemy in rows}
by_id: dict[int, EnemyType] = {enemy.id: enemy for enemy in rows}
