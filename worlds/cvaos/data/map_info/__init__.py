"""
The castle map's cell grid, as the game stores it (the ROM table at 0x08116650).

The grid is 64 cells wide. Rows 0 to 34 are the castle, which is all the game's own map scans
read, and rows 36 to 44 are the Chaotic Realm. Each cell is a u16: the room in bits 0-5, the area
in bits 6-9, a warp room in bit 14 and a save room in bit 15, or 0xFFFF where there is no room. A
cell's room identifier is its area and room in hex, the same as in room_info ("721").

``map_grid.txt`` holds the first 47 rows, one per line, as 64 hex words. Rewrite it from a US ROM
with:

    python -m worlds.cvaos.data.map_info "Castlevania - Aria of Sorrow (USA).gba"
"""
from __future__ import annotations

import os
import pkgutil
import struct
import sys

GRID_ADDRESS = 0x08116650
COLUMNS = 64
ROWS = 47
EMPTY = 0xFFFF
SAVE_ROOM = 0x8000
WARP_ROOM = 0x4000


def _load() -> tuple[tuple[int, ...], ...]:
    text = pkgutil.get_data(__name__, "map_grid.txt").decode("ascii")
    rows = tuple(tuple(int(word, 16) for word in line.split())
                 for line in text.splitlines() if line and not line.startswith("#"))
    if len(rows) != ROWS or any(len(row) != COLUMNS for row in rows):
        raise ValueError(f"map_grid.txt must hold {ROWS} rows of {COLUMNS} cells")
    return rows


grid: tuple[tuple[int, ...], ...] = _load()


def cell(x: int, y: int) -> int:
    """Return the grid's value at column ``x`` and row ``y``, or EMPTY outside the grid."""
    if 0 <= x < COLUMNS and 0 <= y < ROWS:
        return grid[y][x]
    return EMPTY


def room_at(x: int, y: int) -> str | None:
    """Return the identifier of the room at a map cell, or None where there is no room."""
    value = cell(x, y)
    if value == EMPTY:
        return None
    return f"{(value >> 6) & 0xF:X}{value & 0x3F:02X}"


def extract(rom: bytes) -> tuple[tuple[int, ...], ...]:
    """Return the grid's rows as read from a US ROM."""
    start = GRID_ADDRESS - 0x08000000
    return tuple(struct.unpack_from(f"<{COLUMNS}H", rom, start + row * COLUMNS * 2)
                 for row in range(ROWS))


def write(rom_path: str) -> None:
    """Rewrite map_grid.txt from the ROM at ``rom_path``."""
    with open(rom_path, "rb") as f:
        rows = extract(f.read())
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "map_grid.txt")
    with open(path, "w", encoding="ascii", newline="\n") as f:
        f.write("# The castle map's cell grid (ROM 0x08116650). See __init__.py.\n")
        for row in rows:
            f.write(" ".join(f"{value:04X}" for value in row) + "\n")


if __name__ == "__main__":
    write(sys.argv[1])
