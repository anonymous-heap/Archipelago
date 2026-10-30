"""
The parts of the castle Soma has explored, which the client keeps in data storage for the tracker.

The game records two bits for each map cell (ram.addresses.EXPLORED_MAP). A cell becomes visited
and remembered together, the moment Soma enters it. The in-game map draws visited cells in full and
remembered ones dimmed, because a new game that keeps the old file's map clears only the visited
bits. The game keeps 40 rows and never marks a cell in the Chaotic Realm, which its map doesn't
show.

The game's event flags 0 to 12 record the hidden doors Soma has gone through, in the order of
map_info.hidden_doors. The map shows a hidden door as a wall until then.

Each Castle Map Soma holds shows whole areas on the map (CASTLE_MAP_AREAS), drawn dimmed like
remembered cells, except for the secret rooms in map_info.CASTLE_MAP_SKIPS (sub_08010D60).

The client sends these under EXPLORATION_KEY whenever they change: the cell bitmasks as hex, just
as RAM holds them, the hidden-door bits as an int, and the Castle Maps held as an int with bit 0
for Castle Map 1. :func:`decode` turns that back into cells.
"""
from __future__ import annotations

import struct
from typing import NamedTuple

from .data import map_info

EXPLORATION_KEY = "cvaos_explored_{team}_{player}"
ROWS = 40
CELLS_SIZE = 2 * ROWS * 8  # two halves of the columns, each row two u32 bitmasks
HIDDEN_DOOR_FLAGS = (1 << len(map_info.hidden_doors)) - 1

# The areas each Castle Map shows: Castle Corridor and Chapel, then Dance Hall, then Floating
# Garden. The game reveals them only in the castle's 35 rows.
CASTLE_MAP_AREAS: dict[int, tuple[int, ...]] = {1: (0, 1), 2: (3,), 3: (5,)}
CASTLE_ROWS = 35


class Exploration(NamedTuple):
    visited: frozenset[tuple[int, int]]     # map cells, as (column, row)
    remembered: frozenset[tuple[int, int]]
    hidden_doors: frozenset[int]            # indexes into map_info.hidden_doors
    castle_maps: frozenset[int] = frozenset()  # 1, 2 and 3 for the Castle Maps Soma holds


def value(cells: bytes, event_flags: int, castle_maps: bytes = b"") -> dict:
    """Return the data storage value for the explored-cell bitmasks, the event flags, and how
    many of each Castle Map Soma holds."""
    held = sum(1 << index for index, count in enumerate(castle_maps) if count)
    return {"cells": cells.hex(), "hidden_doors": event_flags & HIDDEN_DOOR_FLAGS,
            "castle_maps": held}


def decode(stored) -> Exploration | None:
    """Return the exploration a data storage value records, or None if it isn't one.

    A value from a client that doesn't send the Castle Maps holds none.
    """
    try:
        cells = bytes.fromhex(stored["cells"])
        doors = int(stored["hidden_doors"])
        maps = int(stored.get("castle_maps", 0))
    except (TypeError, KeyError, ValueError, AttributeError):
        return None
    if len(cells) != CELLS_SIZE:
        return None
    visited, remembered = set(), set()
    for half in range(2):
        for row in range(ROWS):
            at = (half * ROWS + row) * 8
            visited_bits, remembered_bits = struct.unpack_from("<II", cells, at)
            for bit in range(32):
                if visited_bits >> bit & 1:
                    visited.add((half * 32 + bit, row))
                if remembered_bits >> bit & 1:
                    remembered.add((half * 32 + bit, row))
    passed = frozenset(index for index in range(len(map_info.hidden_doors)) if doors >> index & 1)
    held = frozenset(number for number in CASTLE_MAP_AREAS if maps >> (number - 1) & 1)
    return Exploration(frozenset(visited), frozenset(remembered), passed, held)


def revealed_by_castle_maps(castle_maps: frozenset[int]) -> frozenset[tuple[int, int]]:
    """Return the map cells that the Castle Maps numbered in ``castle_maps`` show."""
    areas = {area for number in castle_maps for area in CASTLE_MAP_AREAS[number]}
    return frozenset((x, y) for y in range(CASTLE_ROWS) for x in range(map_info.COLUMNS)
                     if map_info.cell(x, y) != map_info.EMPTY
                     and (map_info.cell(x, y) >> 6) & 0xF in areas
                     and (x, y) not in map_info.CASTLE_MAP_SKIPS)


def encode(exploration: Exploration, event_flags: int = 0) -> dict:
    """Return the data storage value for ``exploration``, as the client would send it.

    The inverse of :func:`decode`, for tests and tools that have cells rather than RAM.
    """
    cells = bytearray(CELLS_SIZE)
    for word, marked in ((0, exploration.visited), (4, exploration.remembered)):
        for x, y in marked:
            at = ((x >> 5) * ROWS + y) * 8 + word
            bits = struct.unpack_from("<I", cells, at)[0]
            struct.pack_into("<I", cells, at, bits | 1 << (x & 31))
    doors = sum(1 << index for index in exploration.hidden_doors)
    castle_maps = bytes(int(number in exploration.castle_maps) for number in CASTLE_MAP_AREAS)
    return value(bytes(cells), event_flags & ~HIDDEN_DOOR_FLAGS | doors, castle_maps)
