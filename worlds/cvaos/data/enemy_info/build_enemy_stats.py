"""
Builds ``enemy_stats.csv``: each enemy type's HP and the rarity byte of each of its drop slots.

Generation has no ROM, so Item Drop Shuffle reads these values from the CSV instead. This script
reads them from the USA ROM's enemy stat table (GBA 0x080E9644, 113 rows of 0x24 bytes):

* ``hp`` is the u16 at +0x0C. Spawning an enemy copies it into the entity's HP field at +0x34
  (0x08035F22 and 0x0804CA50). The damage routine at 0x0806E218 subtracts each hit from that
  field and reports the enemy dead once it falls to 0 or below.
* ``drop_1_rarity`` and ``drop_2_rarity`` are the bytes at +0x15 and +0x16. The death routine's
  item roll (0x08068558) loads slot 1's item from +0x08 with its rarity from +0x15, and slot 2's
  item from +0x0A with its rarity from +0x16. The slot's item then drops with chance
  4 / max(16, rarity * 4 + 16 - LCK / 16).

The script checks that the ROM is Aria of Sorrow (USA), and that the item each slot holds matches
``enemy_types.csv``. A wrong table address or row size therefore raises an error instead of writing
wrong values. Run from anywhere: ``python build_enemy_stats.py <path to the USA ROM>``.
"""

from __future__ import annotations

import csv
import struct
import sys
from pathlib import Path
from typing import Dict, List, NamedTuple

HERE = Path(__file__).resolve().parent
TYPES_CSV = HERE / "enemy_types.csv"
ITEM_INFO_CSV = HERE.parent / "item_info" / "item_info.csv"
OUT_CSV = HERE / "enemy_stats.csv"

GBA_ROM_BASE = 0x08000000
GAME_CODE_OFFSET = 0xAC
USA_GAME_CODE = b"A2CE"

TABLE = 0x080E9644
ROW_SIZE = 0x24
ENEMY_COUNT = 113
DROP_1 = 0x08         # u16: item_number + 1, or 0 for an empty slot
DROP_2 = 0x0A
HP = 0x0C             # u16
DROP_1_RARITY = 0x15  # u8
DROP_2_RARITY = 0x16

COLUMNS = ("enemy_id", "name", "hp", "drop_1_rarity", "drop_2_rarity")


class EnemyStatsRow(NamedTuple):
    enemy_id: int
    name: str
    hp: int
    drop_1_rarity: int
    drop_2_rarity: int


def _read(path: Path) -> List[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def read_rows(rom: bytes) -> List[EnemyStatsRow]:
    """Return every enemy type's stats from ``rom``, the bytes of the USA ROM.

    Raises ValueError for another ROM, or when a drop slot holds a different item than
    ``enemy_types.csv`` says.
    """
    if rom[GAME_CODE_OFFSET:GAME_CODE_OFFSET + 4] != USA_GAME_CODE:
        raise ValueError("this is not the Aria of Sorrow (USA) ROM")
    item_names: Dict[int, str] = {int(row["item_number"]): row["name"]
                                  for row in _read(ITEM_INFO_CSV)}
    types = _read(TYPES_CSV)
    if len(types) != ENEMY_COUNT:
        raise ValueError(f"enemy_types.csv lists {len(types)} types, not {ENEMY_COUNT}")
    rows = []
    for enemy_id, enemy in enumerate(types):
        row = TABLE - GBA_ROM_BASE + enemy_id * ROW_SIZE
        stored = struct.unpack_from("<HH", rom, row + DROP_1)
        drops = tuple(item_names[value - 1] if value else "" for value in stored)
        if drops != (enemy["drop_1"], enemy["drop_2"]):
            raise ValueError(f"enemy {enemy_id} ({enemy['name']}) drops {drops} in the ROM, but "
                             f"enemy_types.csv says {(enemy['drop_1'], enemy['drop_2'])}")
        hp = struct.unpack_from("<H", rom, row + HP)[0]
        rows.append(EnemyStatsRow(enemy_id, enemy["name"], hp,
                                  rom[row + DROP_1_RARITY], rom[row + DROP_2_RARITY]))
    return rows


def build(rom_path: Path) -> List[EnemyStatsRow]:
    rows = read_rows(rom_path.read_bytes())
    with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(COLUMNS)
        writer.writerows(rows)
    return rows


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python build_enemy_stats.py <path to the Aria of Sorrow (USA) ROM>")
    written = build(Path(sys.argv[1]))
    print(f"wrote {len(written)} rows to {OUT_CSV}")
