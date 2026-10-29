"""
Map data for Universal Tracker's map tab.

UT draws the map images from the player's CVAoSPT PopTracker pack, a zip set as
``ut_poptracker_path`` in host.yaml, and reads where each location sits from the two JSON files in
``tracker/``. Those files are built from this world's own data by :func:`build`. Running this
module rewrites them, and a test checks that they still match the data.

Each location gets a pin where its pickup sits on the in-game map: its room's map cell, plus its
position within the room, at 24 image pixels per cell. A cell is 256 by 256 pixels of a room, as in
the game's own map-cell routine (sub_0804F7A4). The pack's map images use that scale, and
:data:`MAPS` gives the image position of cell (0, 0) on each. UT shows a section only when its name
is a location the server has, so the Hard Mode pickups appear only in seeds that have them.

    python -m worlds.cvaos.tracker_map
"""
from __future__ import annotations

import json
import os
from typing import NamedTuple

from .data import pickup_info_collection
from .data.room_info import by_identifier as rooms_by_identifier

CELL_PIXELS = 24                 # image pixels per map cell on the pack's map images
ROOM_CELL = (256, 256)           # room pixels per map cell, horizontally and vertically
CHAOTIC_REALM = "Chaotic Realm"  # the region whose rooms have a map image of their own


class TrackerMap(NamedTuple):
    name: str
    img: str                  # path inside the pack zip
    size: tuple[int, int]     # the image's size in the pack's release 0.1
    origin: tuple[int, int]   # image position of map cell (0, 0)
    castle: bool              # shows the castle's rooms
    chaotic: bool             # shows the Chaotic Realm's rooms


MAPS = (
    TrackerMap("Dracula's Castle", "images/maps/Dracula's Castle.png", (1440, 960), (86, 49),
               True, False),
    TrackerMap("Chaotic Realm", "images/maps/Chaotic Realm.png", (1440, 960), (575, -482),
               False, True),
    TrackerMap("Combined Castle", "images/maps/Combined.png", (1440, 1182), (86, 49), True, True),
)

FOLDER = "tracker"
MAPS_FILE = "maps.json"
LOCATIONS_FILE = "locations.json"


def cell_position(room_identifier: str, x: int, y: int) -> tuple[float, float]:
    """Return the map-cell coordinates of a point ``x``, ``y`` pixels into a room."""
    room = rooms_by_identifier[room_identifier]
    return int(room.x) + x / ROOM_CELL[0], int(room.y) + y / ROOM_CELL[1]


def pin(origin: tuple[int, int], cell: tuple[float, float]) -> tuple[int, int]:
    """Return the image position of map-cell coordinates ``cell`` on a map with this ``origin``."""
    return round(origin[0] + CELL_PIXELS * cell[0]), round(origin[1] + CELL_PIXELS * cell[1])


def build() -> tuple[list[dict], list[dict]]:
    """Return the maps file and the locations file, as the JSON values UT reads."""
    maps = [{"name": m.name, "img": m.img, "location_size": 15, "location_border_thickness": 3}
            for m in MAPS]
    locations = []
    for pickup in sorted(pickup_info_collection, key=lambda p: p.ptr_address):
        cell = cell_position(pickup.room_identifier, pickup.x, pickup.y)
        chaotic = rooms_by_identifier[pickup.room_identifier].Region == CHAOTIC_REALM
        map_locations = [dict(zip(("map", "x", "y"), (m.name, *pin(m.origin, cell))))
                         for m in MAPS if (m.chaotic if chaotic else m.castle)]
        locations.append({"name": pickup.room_identifier,
                          "sections": [{"name": pickup.location_name}],
                          "map_locations": map_locations})
    return maps, locations


def write(folder: str) -> None:
    """Write both files into ``folder``."""
    maps, locations = build()
    for name, value in ((MAPS_FILE, maps), (LOCATIONS_FILE, locations)):
        with open(os.path.join(folder, name), "w", encoding="utf-8", newline="\n") as f:
            json.dump(value, f, indent=1)
            f.write("\n")


if __name__ == "__main__":
    write(os.path.join(os.path.dirname(os.path.abspath(__file__)), FOLDER))
