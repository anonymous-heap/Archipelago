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

UT also shows Soma on the map. The client keeps his room and position in data storage under
:data:`POSITION_KEY`, and :func:`position_icon` turns that into an icon at the centre of the map
cell he is in, the cell the in-game map marks.

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


# Soma's position, kept in data storage by the client as [room, x, y]: the room's identifier and
# his position in it, in pixels. UT fills in the team and slot.
POSITION_KEY = "cvaos_position_{team}_{player}"
POSITION_ICON = "images/abilities/backdash.png"   # a Soma sprite in the pack zip
POSITION_ICON_SIZE = 32


def cell_position(room_identifier: str, x: int, y: int) -> tuple[float, float]:
    """Return the map-cell coordinates of a point ``x``, ``y`` pixels into a room."""
    room = rooms_by_identifier[room_identifier]
    return int(room.x) + x / ROOM_CELL[0], int(room.y) + y / ROOM_CELL[1]


def cell_of(value) -> tuple:
    """Return Soma's room and the cell within it for a position value, without looking the room up.

    The client compares these to decide whether the icon would move.
    """
    room_identifier, x, y = value
    return room_identifier, x // ROOM_CELL[0], y // ROOM_CELL[1]


def shows_room(tracker: TrackerMap, room_identifier: str) -> bool:
    """Whether ``tracker`` draws the room ``room_identifier``."""
    chaotic = rooms_by_identifier[room_identifier].Region == CHAOTIC_REALM
    return tracker.chaotic if chaotic else tracker.castle


def position_value(area: int, room: int, x: int, y: int) -> list:
    """Return the data storage value for Soma at ``x``, ``y`` in room ``room`` of area ``area``."""
    return [f"{area:X}{room:02X}", x, y]


def position_icon(map_id, value):
    """Return Soma's icon for map ``map_id``, as ``(x, y, image)``, or None when it has none.

    UT calls this with "" or None before the client has written a position, and with a map_id of
    None before a map has loaded. An exception here would disconnect UT, so anything it cannot place
    returns None instead.
    """
    if not isinstance(map_id, int) or not 0 <= map_id < len(MAPS):
        return None
    try:
        room_identifier, cell_x, cell_y = cell_of(value)
        if not shows_room(MAPS[map_id], room_identifier):
            return None
        room = rooms_by_identifier[room_identifier]
        # The game's map-cell routine (sub_0804F7A4) adds his whole cells within the room to the
        # room's own cell, and the icon sits at that cell's centre.
        centre = (int(room.x) + cell_x + 0.5, int(room.y) + cell_y + 0.5)
        return (*pin(MAPS[map_id].origin, centre), POSITION_ICON)
    except (TypeError, ValueError, KeyError):
        return None


def pin(origin: tuple[int, int], cell: tuple[float, float]) -> tuple[int, int]:
    """Return the image position of map-cell coordinates ``cell`` on a map with this ``origin``."""
    return round(origin[0] + CELL_PIXELS * cell[0]), round(origin[1] + CELL_PIXELS * cell[1])


def build() -> tuple[list[dict], list[dict]]:
    """Return the maps file and the locations file, as the JSON values UT reads."""
    maps = [{"name": m.name, "img": m.img, "location_size": 15, "location_border_thickness": 3,
             "location_icon_size": POSITION_ICON_SIZE} for m in MAPS]
    locations = []
    for pickup in sorted(pickup_info_collection, key=lambda p: p.ptr_address):
        cell = cell_position(pickup.room_identifier, pickup.x, pickup.y)
        map_locations = [dict(zip(("map", "x", "y"), (m.name, *pin(m.origin, cell))))
                         for m in MAPS if shows_room(m, pickup.room_identifier)]
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
