"""
The parts of the castle Soma has explored, which the client keeps in data storage for the tracker.

The game records two bits for each map cell (ram.addresses.EXPLORED_MAP). A cell becomes visited
and remembered together, the moment Soma enters it. The in-game map draws visited cells in full and
remembered ones dimmed, because a new game that keeps the old file's map clears only the visited
bits. The game keeps 40 rows and never marks a cell in the Chaotic Realm, which its map doesn't
show.

The game's event flags 0 to 12 record the hidden doors Soma has gone through, in the order of
map_info.hidden_doors. The map shows a hidden door as a wall until then.

The client sends these under EXPLORATION_KEY whenever they change: the cell bitmasks as hex, just
as RAM holds them, and the hidden-door bits as an int. :func:`decode` turns that back into cells.
"""
from __future__ import annotations

import struct
from typing import NamedTuple

from .data import map_info

EXPLORATION_KEY = "cvaos_explored_{team}_{player}"
ROWS = 40
CELLS_SIZE = 2 * ROWS * 8  # two halves of the columns, each row two u32 bitmasks
HIDDEN_DOOR_FLAGS = (1 << len(map_info.hidden_doors)) - 1


class Exploration(NamedTuple):
    visited: frozenset[tuple[int, int]]     # map cells, as (column, row)
    remembered: frozenset[tuple[int, int]]
    hidden_doors: frozenset[int]            # indexes into map_info.hidden_doors


def value(cells: bytes, event_flags: int) -> dict:
    """Return the data storage value for the explored-cell bitmasks and the event flags."""
    return {"cells": cells.hex(), "hidden_doors": event_flags & HIDDEN_DOOR_FLAGS}


def decode(stored) -> Exploration | None:
    """Return the exploration a data storage value records, or None if it isn't one."""
    try:
        cells = bytes.fromhex(stored["cells"])
        doors = int(stored["hidden_doors"])
    except (TypeError, KeyError, ValueError):
        return None
    if len(cells) != CELLS_SIZE:
        return None
    visited, remembered = set(), set()
    for half in range(2):
        for row in range(ROWS):
            visited_bits, remembered_bits = struct.unpack_from("<II", cells, (half * ROWS + row) * 8)
            for bit in range(32):
                if visited_bits >> bit & 1:
                    visited.add((half * 32 + bit, row))
                if remembered_bits >> bit & 1:
                    remembered.add((half * 32 + bit, row))
    passed = frozenset(index for index in range(len(map_info.hidden_doors)) if doors >> index & 1)
    return Exploration(frozenset(visited), frozenset(remembered), passed)


def encode(exploration: Exploration, event_flags: int = 0) -> dict:
    """Return the data storage value for ``exploration``, as the client would send it.

    The inverse of :func:`decode`, for tests and tools that have cells rather than RAM.
    """
    cells = bytearray(CELLS_SIZE)
    for word, marked in ((0, exploration.visited), (4, exploration.remembered)):
        for x, y in marked:
            at = ((x >> 5) * ROWS + y) * 8 + word
            struct.pack_into("<I", cells, at, struct.unpack_from("<I", cells, at)[0] | 1 << (x & 31))
    doors = sum(1 << index for index in exploration.hidden_doors)
    return value(bytes(cells), event_flags & ~HIDDEN_DOOR_FLAGS | doors)
