"""
The castle map as the tracker page draws it, sent once as /map.json.

All positions here are in map cells: cell (x, y) covers x to x + 1 across and y to y + 1 down, as
in the game's map grid (data/map_info). The page draws the rooms, walls and doors from this data,
and puts each location's pin where its pickup is.
"""
from __future__ import annotations

from ..data import map_info, pickup_info_collection
from ..data.room_info import by_identifier as rooms_by_identifier
from ..locations import location_name_to_id
from ..tracker_map import AREA_NAMES, REALM_AREA, ROOM_CELL, cell_position

EDGE_KINDS = {map_info.HIDDEN_DOOR: "hidden", map_info.DOOR: "door", map_info.WALL: "wall"}


def _cells() -> list[dict]:
    cells = []
    for y in range(map_info.ROWS):
        for x in range(map_info.COLUMNS):
            room = map_info.room_at(x, y)
            if room is None:
                continue
            value = map_info.cell(x, y)
            kind = ("save" if value & map_info.SAVE_ROOM else
                    "warp" if value & map_info.WARP_ROOM else "room")
            cells.append({"x": x, "y": y, "room": room, "area": room[0], "kind": kind})
    return cells


def _edges() -> list[dict]:
    """Return every wall and door, as the cell whose top or left edge it is."""
    edges = []
    for y in range(map_info.ROWS + 1):
        for x in range(map_info.COLUMNS + 1):
            for side, kind in (("top", map_info.top_edge(x, y)),
                               ("left", map_info.left_edge(x, y))):
                if kind != map_info.OPEN:
                    edges.append({"x": x, "y": y, "side": side, "kind": EDGE_KINDS[kind]})
    return edges


def _areas(cells: list[dict]) -> list[dict]:
    """Return each area with its bounds, castle areas first and the Chaotic Realm last."""
    areas = []
    for area in sorted({cell["area"] for cell in cells}, key=lambda a: (a == REALM_AREA, a)):
        xs = [cell["x"] for cell in cells if cell["area"] == area]
        ys = [cell["y"] for cell in cells if cell["area"] == area]
        areas.append({"id": area, "name": AREA_NAMES[area], "realm": area == REALM_AREA,
                      "bounds": [min(xs), min(ys), max(xs) + 1, max(ys) + 1]})
    return areas


def _locations() -> list[dict]:
    locations = []
    for pickup in sorted(pickup_info_collection, key=lambda p: p.ptr_address):
        x, y = cell_position(pickup.room_identifier, pickup.x, pickup.y)
        locations.append({"id": location_name_to_id[pickup.location_name],
                          "name": pickup.location_name, "room": pickup.room_identifier,
                          "area": pickup.room_identifier[0], "x": round(x, 3), "y": round(y, 3)})
    return locations


def map_data() -> dict:
    """Return the map for the tracker's page: rooms, walls, doors, areas and location pins."""
    cells = _cells()
    return {
        "columns": map_info.COLUMNS,
        "rows": map_info.ROWS,
        # Each room's top-left map cell, and the room pixels per cell, so that the page can place
        # Soma from his position in his room.
        "rooms": {room: [int(info.x), int(info.y)] for room, info in rooms_by_identifier.items()},
        "room_cell": list(ROOM_CELL),
        "cells": cells,
        "edges": _edges(),
        "hidden_doors": [{"side": door.side, "x": door.x, "y": door.y}
                         for door in map_info.hidden_doors],
        "areas": _areas(cells),
        "locations": _locations(),
    }
