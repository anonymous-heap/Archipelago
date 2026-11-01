"""
The in-game map's cells, edges and hidden doors, as the ROM stores them.

The grid (the ROM table at 0x08116650) is 64 cells wide. Rows 0 to 34 are the castle and rows 36
to 44 are the Chaotic Realm. The game's own map scans read only the castle rows. Each cell is a
u16, or 0xFFFF where there is no room. Bits 0-5 hold the room and bits 6-9 the area, while bit 14
marks a warp room and bit 15 a save room. A cell's room identifier is its area and room in hex, as
in room_info: "721" is room 0x21 of area 7.

The edge table (0x08117DD0, right after the grid) has a nibble for each cell. Bits 2-3 give the
kind of edge along the cell's top and bits 0-1 the kind along its left, so every edge between two
cells is stored once. The game's map routine (sub_08010C4C) draws a cell's top and left edges, then
the top edge of the cell below it and the left edge of the cell to its right.

The hidden-door list (0x081183B0) gives each hidden door's edge as a pair of signed bytes. A y of
0 or more names the top edge of cell (x, y), and a negative y the left edge of cell (x, -y). The
list ends at a negative x. A door's place in the list is its bit in the RAM flags that record the
hidden doors Soma has gone through.

``map_grid.txt`` holds the grid's first 47 rows, one per line, as 64 hex words. ``map_edges.txt``
holds the edge table's 47 rows, one per line, with one hex digit per cell. ``hidden_doors.txt``
holds the hidden-door list, one pair per line. Rewrite all three from a US ROM with:

    python -m worlds.cvaos.data.map_info "Castlevania - Aria of Sorrow (USA).gba"
"""
from __future__ import annotations

import os
import pkgutil
import struct
from typing import NamedTuple

GRID_ADDRESS = 0x08116650
EDGES_ADDRESS = 0x08117DD0
HIDDEN_DOORS_ADDRESS = 0x081183B0
CASTLE_MAP_SKIPS_ADDRESS = 0x084F1054

# The cells a Castle Map leaves out when it reveals its areas: secret rooms in Dance Hall (301,
# 310), Castle Corridor (00B) and Chapel (109). The ROM lists them as (x, y) byte pairs, ending at
# a 0, 0 pair.
CASTLE_MAP_SKIPS: tuple[tuple[int, int], ...] = ((4, 13), (12, 13), (13, 13), (15, 22), (47, 20))
COLUMNS = 64
ROWS = 47
EMPTY = 0xFFFF
SAVE_ROOM = 0x8000
WARP_ROOM = 0x4000

# The kinds of edge. A hidden door shows as a wall on the game's map until Soma has gone through
# it, and as a door after that.
OPEN = 0
HIDDEN_DOOR = 1
DOOR = 2
WALL = 3


class HiddenDoor(NamedTuple):
    side: str  # "top" or "left": which edge of the cell at x, y the door is on
    x: int
    y: int


def _rows(file_name: str) -> list[str]:
    text = pkgutil.get_data(__name__, file_name).decode("ascii")
    return [line for line in text.splitlines() if line and not line.startswith("#")]


def _load_grid() -> tuple[tuple[int, ...], ...]:
    rows = tuple(tuple(int(word, 16) for word in line.split()) for line in _rows("map_grid.txt"))
    if len(rows) != ROWS or any(len(row) != COLUMNS for row in rows):
        raise ValueError(f"map_grid.txt must hold {ROWS} rows of {COLUMNS} cells")
    return rows


def _load_edges() -> tuple[tuple[int, ...], ...]:
    rows = tuple(tuple(int(digit, 16) for digit in line) for line in _rows("map_edges.txt"))
    if len(rows) != ROWS or any(len(row) != COLUMNS for row in rows):
        raise ValueError(f"map_edges.txt must hold {ROWS} rows of {COLUMNS} cells")
    return rows


def _hidden_door(x: int, y: int) -> HiddenDoor:
    return HiddenDoor("left", x, -y) if y < 0 else HiddenDoor("top", x, y)


def _load_hidden_doors() -> tuple[HiddenDoor, ...]:
    return tuple(_hidden_door(*map(int, line.split())) for line in _rows("hidden_doors.txt"))


grid: tuple[tuple[int, ...], ...] = _load_grid()
edges: tuple[tuple[int, ...], ...] = _load_edges()
hidden_doors: tuple[HiddenDoor, ...] = _load_hidden_doors()  # in the game's order


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


def top_edge(x: int, y: int) -> int:
    """Return the kind of edge between the cell at ``x``, ``y`` and the cell above it.

    Outside the table there are no rooms, so the edge is OPEN.
    """
    if 0 <= x < COLUMNS and 0 <= y < ROWS:
        return edges[y][x] >> 2
    return OPEN


def left_edge(x: int, y: int) -> int:
    """Return the kind of edge between the cell at ``x``, ``y`` and the cell to its left.

    Outside the table there are no rooms, so the edge is OPEN.
    """
    if 0 <= x < COLUMNS and 0 <= y < ROWS:
        return edges[y][x] & 3
    return OPEN


def extract(rom: bytes) -> tuple[tuple[int, ...], ...]:
    """Return the grid's rows as read from a US ROM."""
    start = GRID_ADDRESS - 0x08000000
    return tuple(struct.unpack_from(f"<{COLUMNS}H", rom, start + row * COLUMNS * 2)
                 for row in range(ROWS))


def extract_edges(rom: bytes) -> tuple[tuple[int, ...], ...]:
    """Return the edge table's rows as read from a US ROM, with one value per cell."""
    start = EDGES_ADDRESS - 0x08000000
    row_bytes = COLUMNS // 2
    return tuple(tuple(digit for byte in rom[start + row * row_bytes:start + (row + 1) * row_bytes]
                       for digit in (byte >> 4, byte & 0xF))
                 for row in range(ROWS))


def extract_hidden_doors(rom: bytes) -> tuple[HiddenDoor, ...]:
    """Return the hidden-door list as read from a US ROM."""
    at = HIDDEN_DOORS_ADDRESS - 0x08000000
    doors = []
    while (x := struct.unpack_from("<b", rom, at)[0]) >= 0:
        doors.append(_hidden_door(x, struct.unpack_from("<b", rom, at + 1)[0]))
        at += 2
    return tuple(doors)


def extract_castle_map_skips(rom: bytes) -> tuple[tuple[int, int], ...]:
    """Return the cells the Castle Maps leave out, as read from a US ROM."""
    at = CASTLE_MAP_SKIPS_ADDRESS - 0x08000000
    cells = []
    while rom[at]:
        cells.append((rom[at], rom[at + 1]))
        at += 2
    return tuple(cells)


def write(rom_path: str) -> None:
    """Rewrite map_grid.txt, map_edges.txt and hidden_doors.txt from the ROM at ``rom_path``."""
    with open(rom_path, "rb") as f:
        rom = f.read()
    folder = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(folder, "map_grid.txt"), "w", encoding="ascii", newline="\n") as f:
        f.write("# The castle map's cell grid (ROM 0x08116650). See __init__.py.\n")
        for row in extract(rom):
            f.write(" ".join(f"{value:04X}" for value in row) + "\n")
    with open(os.path.join(folder, "map_edges.txt"), "w", encoding="ascii", newline="\n") as f:
        f.write("# The castle map's edge table (ROM 0x08117DD0). See __init__.py.\n")
        for row in extract_edges(rom):
            f.write("".join(f"{value:X}" for value in row) + "\n")
    with open(os.path.join(folder, "hidden_doors.txt"), "w", encoding="ascii", newline="\n") as f:
        f.write("# The castle map's hidden-door list (ROM 0x081183B0). See __init__.py.\n")
        for door in extract_hidden_doors(rom):
            f.write(f"{door.x} {-door.y if door.side == 'left' else door.y}\n")
