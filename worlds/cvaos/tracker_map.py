"""
Map data for Universal Tracker's map tab.

UT shows the maps in ``tracker/``, with a pin for every location and a marker for Soma.
:func:`write` generates every file there from this world's own data. The images are schematics
drawn from the castle's cell grid and edge table (``data/map_info``), so no game art is shipped.
Each pin is placed from its pickup's room and position.

There is a map of the whole castle, one of the Chaotic Realm, and one for each area of the castle.
An area's map shows the rest of the castle dimmed around it. A room's area is the first digit of its
identifier. Each map uses 24 image pixels per map cell, and a cell covers 256 by 256 pixels of a
room, as it does in the game's own map-cell routine (sub_0804F7A4). UT shows a pin only for a
location the server has, so the Hard Mode pickups appear only in seeds that have them.

The client keeps Soma's room and position in data storage. UT reads them from
:data:`MAP_CELL_KEY`, which changes only when he enters another map cell. :func:`position_icon`
marks the cell he is in, as the in-game map does, and :func:`map_page_index` switches UT to the
map of his area.

A test checks that the shipped files match this world's data. Running this module rewrites them::

    python -m worlds.cvaos.tracker_map
"""
from __future__ import annotations

import functools
import json
import math
import os
from typing import NamedTuple

from . import tracker_png
from .data import map_info, pickup_info_collection
from .enemy_checks import checkable_types, defeat_location_name, pin_rooms
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
# his position in it, in pixels. The client updates it as he moves, and the tracker's map reads it.
POSITION_KEY = "cvaos_position_{team}_{player}"
# The same value, updated only when Soma enters another map cell. UT's map reads this key, because
# it marks only the cell and reloads its map whenever the key changes. UT fills in the team and
# slot.
MAP_CELL_KEY = "cvaos_map_cell_{team}_{player}"

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
    bounds: tuple[int, int, int, int]  # first column and row drawn, then last, inclusive
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

    Each cell is filled in its room's colour. The walls and doors come from the game's edge table.
    Every wall and door is centred on the line between two cells, so a wall that two rooms share is
    drawn once. A hidden door is drawn as a door, which is how the game's map shows it once Soma has
    gone through it.
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


# The least distance, in map cells, between a defeat pin and any other pin. UT draws a pin 12 pixels
# across, half a cell, and the browser tracker's pins are a little smaller.
PIN_SPACING = 0.5


# The offsets tried for a pin that would cover another, nearest first: points an eighth of a cell
# apart, up to a cell and a half away.
_OFFSETS = sorted(((dx / 8, dy / 8) for dx in range(-12, 13) for dy in range(-12, 13)),
                  key=lambda offset: (math.hypot(*offset), offset))


@functools.cache
def defeat_pins() -> tuple[tuple[str, tuple[tuple[str, tuple[float, float]], ...]], ...]:
    """Return each defeat location's pins, as its name and a (room, map-cell position) per pin.

    A defeat location has a pin in each room where its enemy appears (enemy_checks.pin_rooms). The
    pin starts at the enemy's placement in that room, or in the middle of the room's first map
    cell when no placement there has a recorded position.

    A pin moves when it would come within PIN_SPACING of another pin and hide part of it. The other
    pins are the pickups', the defeat pins already placed, and the starting spots of those still
    to come. The pin then moves to the nearest spot that is clear of them all and keeps the whole
    pin inside its room. In a room too crowded for that, it goes where it comes closest to being
    clear.
    """
    starts = []
    for enemy in checkable_types():
        for room, placement in pin_rooms(enemy.name):
            start = (cell_position(room, placement.x, placement.y) if placement is not None
                     else _first_cell_middle(room))
            starts.append((defeat_location_name(enemy.name), room, start))
    placed = [cell_position(pickup.room_identifier, pickup.x, pickup.y)
              for pickup in pickup_info_collection]
    pins: dict[str, list[tuple[str, tuple[float, float]]]] = {}
    for index, (name, room, start) in enumerate(starts):
        waiting = (cell for _, _, cell in starts[index + 1:])
        others = [cell for cell in (*placed, *waiting) if math.dist(start, cell) < 2]
        cell = _free_spot(room, start, others)
        placed.append(cell)
        pins.setdefault(name, []).append((room, cell))
    return tuple((name, tuple(spots)) for name, spots in pins.items())


@functools.cache
def _cells_by_room() -> dict[str, list[tuple[int, int]]]:
    """Return each room's map cells, top row first and left to right."""
    cells: dict[str, list[tuple[int, int]]] = {}
    for y in range(map_info.ROWS):
        for x in range(map_info.COLUMNS):
            room = map_info.room_at(x, y)
            if room is not None:
                cells.setdefault(room, []).append((x, y))
    return cells


def _first_cell_middle(room: str) -> tuple[float, float]:
    x, y = _cells_by_room()[room][0]
    return x + 0.5, y + 0.5


def _free_spot(room: str, start: tuple[float, float],
               others: list[tuple[float, float]]) -> tuple[float, float]:
    def gap(spot: tuple[float, float]) -> float:
        return min((math.dist(spot, other) for other in others), default=math.inf)

    if gap(start) >= PIN_SPACING:
        return start
    inside = [spot for spot in ((start[0] + dx, start[1] + dy) for dx, dy in _OFFSETS)
              if _pin_inside(room, spot)]
    return next((spot for spot in inside if gap(spot) >= PIN_SPACING),
                max(inside, key=gap, default=start))


def _pin_inside(room: str, spot: tuple[float, float]) -> bool:
    """Whether a pin centred at ``spot`` lies wholly in map cells of ``room``."""
    half = PIN_SPACING / 2
    return all(map_info.room_at(math.floor(spot[0] + dx), math.floor(spot[1] + dy)) == room
               for dx in (-half, half) for dy in (-half, half))


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
    """Return Soma's marker on map ``map_id`` as ``(x, y, image)``, or None when there is none.

    A map marks Soma only in a room it draws in full colour. UT calls this with "" or None before
    the client has written a position, and with a map_id of None before a map has loaded. An
    exception here would disconnect UT, so this returns None for any value it cannot place.
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

    UT calls this with the same value as :func:`position_icon`. For a castle room this is its
    area's map, and for a Chaotic Realm room it is the Realm's map. Before the client has stored a
    position, such as before the game starts, this returns the whole castle's map, because UT shows
    no map at all until one is selected.
    """
    if value is None or value == "":
        return 0
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
    # Each defeat location is pinned where defeat_pins puts it, once in each room where its enemy
    # appears. UT shows it only in a seed that has it. UT keys a map's pins by position, so a pin
    # that would land on a taken spot moves right, a pixel at a time, until the spot is free.
    taken = {(spot["map"], spot["x"], spot["y"])
             for node in locations for spot in node["map_locations"]}
    for name, spots in defeat_pins():
        map_locations = []
        for room, cell in spots:
            for m in MAPS:
                if room not in m.rooms:
                    continue
                x, y = pin(m.origin, cell)
                while (m.name, x, y) in taken:
                    x += 1
                taken.add((m.name, x, y))
                map_locations.append({"map": m.name, "x": x, "y": y})
        locations.append({"name": name, "sections": [{"name": name}],
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
