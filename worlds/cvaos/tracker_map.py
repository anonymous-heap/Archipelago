"""
Map data for Universal Tracker's map tab.

UT shows the maps in ``tracker/``, with a pin for every location and a marker for Soma. All of it is
generated from this world's own data by :func:`write`. The images are schematics drawn from the
castle's cell grid and edge table (``data/map_info``), and each pin comes from its pickup's room and
position, so no game art is shipped. Running this module rewrites the files, and a test checks that
they match.

There is a map of the castle, one of the Chaotic Realm, and one for each of the game's areas that
shows the rest of the castle dimmed around it. A room's area is the first digit of its identifier.
Each map draws 24 pixels per map cell, and a cell is 256 by 256 pixels of a room, as in the game's
own map-cell routine (sub_0804F7A4). UT shows a pin only for a location the server has, so the
Hard Mode pickups appear only in seeds that have them.

The client keeps Soma's room and position in data storage under :data:`POSITION_KEY`.
:func:`position_icon` marks the cell he is in, as the in-game map does, and :func:`map_page_index`
switches UT to the map of his area.

    python -m worlds.cvaos.tracker_map
"""
from __future__ import annotations

import json
import os
from typing import NamedTuple

from . import tracker_png
from .data import map_info, pickup_info_collection
from .data._csv_resources import open_csv
from .data.room_info import by_identifier as rooms_by_identifier

CELL_PIXELS = 24                 # image pixels per map cell
ROOM_CELL = (256, 256)           # room pixels per map cell, horizontally and vertically
MARGIN = 1                       # cells of background around each map
WALL_PIXELS = 4                  # a wall's thickness, centred on the line between two cells
MARKER_PIXELS = 3                # the thickness of Soma's marker
REALM_AREA = "B"                 # the area digit of the Chaotic Realm's rooms

FOLDER = "tracker"
MAPS_FILE = "maps.json"
LOCATIONS_FILE = "locations.json"
MARKER_FILE = "marker.png"

# Soma's position, kept in data storage by the client as [room, x, y]: the room's identifier and
# his position in it, in pixels. UT fills in the team and slot.
POSITION_KEY = "cvaos_position_{team}_{player}"

BACKGROUND = (46, 0, 118)


class Palette(NamedTuple):
    room: tuple[int, int, int]
    save: tuple[int, int, int]
    warp: tuple[int, int, int]
    wall: tuple[int, int, int]
    door: tuple[int, int, int]


BRIGHT = Palette((0, 0, 230), (230, 30, 30), (240, 230, 30), (255, 255, 255), (0, 200, 220))
DIM = Palette((20, 20, 110), (100, 30, 80), (110, 105, 60), (100, 100, 150), (40, 90, 110))
DOOR_PIXELS = CELL_PIXELS // 2   # a door's length along the wall
MARKER = (255, 140, 0, 255)


class TrackerMap(NamedTuple):
    name: str
    img: str
    bounds: tuple[int, int, int, int]  # first and last cell column and row drawn, inclusive
    rooms: frozenset[str]              # rooms drawn in full colour, with pins and Soma's marker
    context: frozenset[str]            # other rooms drawn dimmed around them

    @property
    def origin(self) -> tuple[int, int]:
        """Return the image position of map cell (0, 0)."""
        return (MARGIN - self.bounds[0]) * CELL_PIXELS, (MARGIN - self.bounds[1]) * CELL_PIXELS

    @property
    def size(self) -> tuple[int, int]:
        """Return the image's width and height in pixels."""
        first_x, first_y, last_x, last_y = self.bounds
        return ((last_x - first_x + 1 + 2 * MARGIN) * CELL_PIXELS,
                (last_y - first_y + 1 + 2 * MARGIN) * CELL_PIXELS)


# Each area's name, as the location names give it ("Dance Hall" for the 3xx rooms).
AREA_NAMES: dict[str, str] = {row["room_identifier"][0]: row["region"] for row in open_csv(
    "worlds.cvaos.data.pickup_info", "friendly_location_names.csv")}


def _bounds(rooms: frozenset[str], pad: int, within: tuple[int, int, int, int] | None = None):
    cells = [(x, y) for y in range(map_info.ROWS) for x in range(map_info.COLUMNS)
             if map_info.room_at(x, y) in rooms]
    first_x, first_y = min(x for x, _ in cells) - pad, min(y for _, y in cells) - pad
    last_x, last_y = max(x for x, _ in cells) + pad, max(y for _, y in cells) + pad
    if within is not None:
        first_x, first_y = max(first_x, within[0]), max(first_y, within[1])
        last_x, last_y = min(last_x, within[2]), min(last_y, within[3])
    return first_x, first_y, last_x, last_y


def _image_name(name: str) -> str:
    return name.lower().replace("the ", "").replace(" ", "_") + ".png"


def _make_maps() -> tuple[TrackerMap, ...]:
    realm = frozenset(room for room in rooms_by_identifier if room[0] == REALM_AREA)
    castle = frozenset(rooms_by_identifier) - realm
    castle_bounds = _bounds(castle, 0)
    realm_name = AREA_NAMES[REALM_AREA]
    maps = [TrackerMap("Castle", "castle.png", castle_bounds, castle, frozenset()),
            TrackerMap(realm_name, _image_name(realm_name), _bounds(realm, 0), realm, frozenset())]
    for area in sorted({room[0] for room in castle}):
        rooms = frozenset(room for room in castle if room[0] == area)
        maps.append(TrackerMap(AREA_NAMES[area], _image_name(AREA_NAMES[area]),
                               _bounds(rooms, 1, castle_bounds), rooms, castle - rooms))
    return tuple(maps)


MAPS: tuple[TrackerMap, ...] = _make_maps()


AREA_MAPS = range(2, len(MAPS))
MAP_GROUPS = [MAPS[0].name, MAPS[1].name, ("Areas", [MAPS[index].name for index in AREA_MAPS])]


def render(tracker: TrackerMap) -> bytes:
    """Return the PNG image of ``tracker``.

    Each cell is filled in its room's colour. The walls and doors come from the game's edge table,
    and each is centred on the line between two cells, so a wall that two rooms share is drawn
    once. A hidden door is drawn as a door, which is how the game's map shows it once Soma has gone
    through it.
    """
    width, height = tracker.size
    canvas = bytearray(bytes(BACKGROUND) * (width * height))

    def fill(left: int, top: int, right: int, bottom: int, colour: tuple[int, int, int]) -> None:
        row = bytes(colour) * (right - left)
        for y in range(top, bottom):
            start = (y * width + left) * 3
            canvas[start:start + len(row)] = row

    origin_x, origin_y = tracker.origin
    first_x, first_y, last_x, last_y = tracker.bounds

    def edge_palette(kind: int, *cells: tuple[int, int]) -> Palette | None:
        """Return the palette for an edge of this kind between ``cells``, or None to leave it out.

        An open edge is not drawn, and neither is one with no drawn room on either side.
        """
        if kind == map_info.OPEN:
            return None
        rooms = {map_info.room_at(x, y) for x, y in cells
                 if first_x <= x <= last_x and first_y <= y <= last_y}
        if rooms & tracker.rooms:
            return BRIGHT
        if rooms & tracker.context:
            return DIM
        return None

    for y in range(first_y, last_y + 1):
        for x in range(first_x, last_x + 1):
            room = map_info.room_at(x, y)
            if room not in tracker.rooms and room not in tracker.context:
                continue
            palette = BRIGHT if room in tracker.rooms else DIM
            value = map_info.cell(x, y)
            colour = (palette.save if value & map_info.SAVE_ROOM else
                      palette.warp if value & map_info.WARP_ROOM else palette.room)
            left, top = origin_x + x * CELL_PIXELS, origin_y + y * CELL_PIXELS
            fill(left, top, left + CELL_PIXELS, top + CELL_PIXELS, colour)

    # Point (x, y) is the top-left corner of cell (x, y). The edge to its right is that cell's top,
    # and the edge below it is that cell's left.
    half = WALL_PIXELS // 2
    door_start = (CELL_PIXELS - DOOR_PIXELS) // 2
    for y in range(first_y, last_y + 2):
        for x in range(first_x, last_x + 2):
            line_x, line_y = origin_x + x * CELL_PIXELS, origin_y + y * CELL_PIXELS
            top = map_info.top_edge(x, y)
            top_palette = edge_palette(top, (x, y - 1), (x, y))
            if top_palette is not None:
                fill(line_x, line_y - half, line_x + CELL_PIXELS, line_y + half, top_palette.wall)
                if top != map_info.WALL:
                    fill(line_x + door_start, line_y - half,
                         line_x + door_start + DOOR_PIXELS, line_y + half, top_palette.door)
            left = map_info.left_edge(x, y)
            left_palette = edge_palette(left, (x - 1, y), (x, y))
            if left_palette is not None:
                fill(line_x - half, line_y, line_x + half, line_y + CELL_PIXELS, left_palette.wall)
                if left != map_info.WALL:
                    fill(line_x - half, line_y + door_start,
                         line_x + half, line_y + door_start + DOOR_PIXELS, left_palette.door)
            # Fill the square where the point's four edges meet, so a wall that turns there has no
            # notch in its corner.
            corner = {top_palette, left_palette,
                      edge_palette(map_info.top_edge(x - 1, y), (x - 1, y - 1), (x - 1, y)),
                      edge_palette(map_info.left_edge(x, y - 1), (x - 1, y - 1), (x, y - 1))}
            corner.discard(None)
            if corner:
                colour = BRIGHT.wall if BRIGHT in corner else DIM.wall
                fill(line_x - half, line_y - half, line_x + half, line_y + half, colour)
    return tracker_png.encode(width, height, bytes(canvas))


def render_marker() -> bytes:
    """Return Soma's marker: an outline the size of one map cell."""
    clear = (0, 0, 0, 0)
    edge = CELL_PIXELS - 1
    pixels = b"".join(bytes(MARKER if min(x, y, edge - x, edge - y) < MARKER_PIXELS else clear)
                      for y in range(CELL_PIXELS) for x in range(CELL_PIXELS))
    return tracker_png.encode(CELL_PIXELS, CELL_PIXELS, pixels, alpha=True)


def cell_position(room_identifier: str, x: int, y: int) -> tuple[float, float]:
    """Return the map-cell coordinates of a point ``x``, ``y`` pixels into a room."""
    room = rooms_by_identifier[room_identifier]
    return int(room.x) + x / ROOM_CELL[0], int(room.y) + y / ROOM_CELL[1]


def pin(origin: tuple[int, int], cell: tuple[float, float]) -> tuple[int, int]:
    """Return the image position of map-cell coordinates ``cell`` on a map with this ``origin``."""
    return round(origin[0] + CELL_PIXELS * cell[0]), round(origin[1] + CELL_PIXELS * cell[1])


def position_value(area: int, room: int, x: int, y: int) -> list:
    """Return the data storage value for Soma at ``x``, ``y`` in room ``room`` of area ``area``."""
    return [f"{area:X}{room:02X}", x, y]


def cell_of(value) -> tuple:
    """Return Soma's room and the cell within it for a position value, without looking the room up.

    The client compares these to decide whether the marker would move.
    """
    room_identifier, x, y = value
    return room_identifier, x // ROOM_CELL[0], y // ROOM_CELL[1]


def position_icon(map_id, value):
    """Return Soma's marker for map ``map_id``, as ``(x, y, image)``, or None when it has none.

    UT calls this with "" or None before the client has written a position, and with a map_id of
    None before a map has loaded. An exception here would disconnect UT, so anything it cannot place
    returns None instead.
    """
    if not isinstance(map_id, int) or not 0 <= map_id < len(MAPS):
        return None
    try:
        room_identifier, cell_x, cell_y = cell_of(value)
        tracker = MAPS[map_id]
        if room_identifier not in tracker.rooms:
            return None
        room = rooms_by_identifier[room_identifier]
        # The game's map-cell routine adds his whole cells within the room to the room's own cell.
        centre = (int(room.x) + cell_x + 0.5, int(room.y) + cell_y + 0.5)
        return (*pin(tracker.origin, centre), MARKER_FILE)
    except (TypeError, ValueError, KeyError):
        return None


def map_page_index(value) -> int:
    """Return the index of the map for Soma's area, or -1 to keep the current map.

    UT calls this with the same value as :func:`position_icon`. A castle room selects its area's
    map, and a Chaotic Realm room selects the Chaotic Realm.
    """
    try:
        room_identifier = cell_of(value)[0]
        return next(index for index in (*AREA_MAPS, 1) if room_identifier in MAPS[index].rooms)
    except (TypeError, ValueError, StopIteration):
        return -1


def build() -> tuple[list[dict], list[dict]]:
    """Return the maps file and the locations file, as the JSON values UT reads."""
    maps = [{"name": m.name, "img": m.img, "location_size": 12, "location_border_thickness": 2,
             "location_icon_size": CELL_PIXELS} for m in MAPS]
    locations = []
    for pickup in sorted(pickup_info_collection, key=lambda p: p.ptr_address):
        cell = cell_position(pickup.room_identifier, pickup.x, pickup.y)
        map_locations = [dict(zip(("map", "x", "y"), (m.name, *pin(m.origin, cell))))
                         for m in MAPS if pickup.room_identifier in m.rooms]
        locations.append({"name": pickup.room_identifier,
                          "sections": [{"name": pickup.location_name}],
                          "map_locations": map_locations})
    return maps, locations


def files() -> dict[str, bytes]:
    """Return every file in tracker/, by name."""
    maps, locations = build()
    out = {name: (json.dumps(value, indent=1) + "\n").encode("utf-8")
           for name, value in ((MAPS_FILE, maps), (LOCATIONS_FILE, locations))}
    out.update({m.img: render(m) for m in MAPS})
    out[MARKER_FILE] = render_marker()
    return out


def write(folder: str) -> None:
    """Write every file in tracker/ into ``folder``."""
    os.makedirs(folder, exist_ok=True)
    for name, data in files().items():
        with open(os.path.join(folder, name), "wb") as f:
            f.write(data)


if __name__ == "__main__":
    write(os.path.join(os.path.dirname(os.path.abspath(__file__)), FOLDER))
