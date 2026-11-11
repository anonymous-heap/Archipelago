"""
Find where Soma lands inside each door, and the TeleportPlayer arguments that put him there.

Door ``"R:N"`` is the door in room R that leads to room N, and its landing is the spot just inside
that door in R. Soma reaches that spot by coming from N through the reverse door ``"N:R"``, so the
landing is computed for that trip. :func:`teleport_args` returns the landing of one door, and
:func:`all_landings` returns the landing of every door in ``entrance_info.csv``.
:func:`door_flags` returns the game flags that follow from coming in by a door, which are to be
set before Soma is put at its landing. :func:`exits` returns the doors of one room, each with the
line that Soma crosses to leave by it.

Reading a room's collision is a port of DSVEdit (https://github.com/LagoLunatic/DSVEdit, MIT
License, copyright LagoLunatic), following only its paths for the US release of Castlevania: Aria
of Sorrow on the GBA. The pieces are ``RoomLayer`` and ``BGLayer`` (dsvlib/layer.rb),
``CollisionTileset`` and ``CollisionTile`` (dsvlib/tileset.rb), and
``Renderer#render_collision_tileset`` and ``Renderer#render_collision_tile`` (dsvlib/renderer.rb).
The camera rules are a port of ``Game#start_test_room`` (dsvlib/game.rb). Each function names the
Ruby methods it comes from.

A room's collision comes from its first layer. That layer is a grid of 32x32-pixel tiles, and each
tile is 4x4 collision cells of 8x8 pixels, with one byte per cell. A screen is 256x256 pixels, so it
holds 32x32 cells.

The game moves Soma through a door when his feet leave the room's playable box. The box's left
edge is x 0, and its right edge is the room's width in pixels, or 240 for a room one screen wide.
Its top edge is y 48, and its bottom edge is the room's height in pixels minus 48. So collision in
the top and bottom 48 pixels of a room is never floor that Soma can stand on, and every landing
lies inside the box with a margin.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import NamedTuple, Optional

from ...data import entrance_info
from . import emulator, settings
from .rooms import SCREEN, TILES_PER_SCREEN, _decompress, _unpack

CELL = 8  # one collision byte covers 8x8 pixels
CELLS_PER_TILE = 4
CELLS_PER_SCREEN = SCREEN // CELL  # 32

GBA_WIDTH, GBA_HEIGHT = 240, 160
# The game moves Soma out of a room when his feet come within this many pixels of its top or bottom.
EXIT_MARGIN = 48
# A room one screen wide has its right exit line here, instead of at x 256.
NARROW_RIGHT = 240
# How far a landing stays inside the side exit lines and the top exit line.
INSET = 16
# How far above the bottom exit line Soma waits at a bottom door with no floor above it.
BOTTOM_INSET = 16
# The height above Soma's feet that must be free of solid collision.
BODY_HEIGHT = 32
# A side door's floor may lie this far below the bottom of the doorway and still count as its floor,
# as it does where the floor slopes down away from the door.
SIDE_FLOOR_SLACK = 16
# A flat floor holds Soma up while one of its pixels is within this many pixels of his feet's x, on
# either side. Measured in the game at the ends of solid blocks and of platforms: he stands with
# his feet 5 px past the end, and falls at 6 px.
FOOT_REACH = 5
# A boss door or a wall that stands in a doorway is within this many pixels of the line that Soma
# crosses there. The game's boss doors are 8 px inside that line, and its walls 0 to 16 px.
DOORWAY_DEPTH = 24

# Collision classes. ``OTHER`` holds damage blocks, plus any shape DSVEdit draws that is neither
# standable nor water.
SOLID, PLATFORM, SLOPE = "solid", "platform", "slope"
WATER, OTHER, EMPTY = "water", "other", "empty"
_BLOCKING = (SOLID, SLOPE)
_FLOOR = (SOLID, SLOPE, PLATFORM)
_FLAT = (SOLID, PLATFORM)

_OPPOSITE = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
# The step from a door's outside cell back to a cell of its own room, for each edge it is beyond.
_INWARD = {"left": (1, 0), "right": (-1, 0), "top": (0, 1), "bottom": (0, -1)}


# --- Collision ------------------------------------------------------------------------------------

def classify(byte: int) -> str:
    """Return the collision class of one collision byte.

    The bits follow CollisionTile#initialize. Bit 0 is ``has_top``, bit 3 is ``is_water``, and bits
    4-7 are ``block_shape``. For shapes 0-3, bit 1 is ``has_sides_and_bottom`` and bit 2 is
    ``has_effect``. For shapes 4 and up, the block is a slope, and bits 1 and 2 are its vertical and
    horizontal flips.

    The classes follow what Renderer#render_collision_tile draws. Every slope shape is a slope,
    except that a water slope without ``has_top`` is water. A shape 0-3 block with both ``has_top``
    and ``has_sides_and_bottom`` is solid, and one with only ``has_top`` is a platform that Soma can
    jump up through. One with only ``has_sides_and_bottom`` is drawn as a solid notched block, so it
    is solid too. Otherwise the water bit makes the cell water, and the effect bit makes it
    ``OTHER``.
    """
    shape = byte >> 4
    has_top = byte & 1
    water = byte & 8
    if shape >= 4:
        return WATER if water and not has_top else SLOPE
    sides = byte & 2
    effect = byte & 4
    if has_top and sides:
        return SOLID
    if has_top:
        return PLATFORM
    if sides:
        return SOLID
    if water:
        return WATER
    if effect:
        return OTHER
    return EMPTY


def _in_triangle(px: float, py: float, a, b, c) -> bool:
    """Return True when the point is inside the triangle or within half a pixel of its edges."""
    def side(p, q):
        return (q[0] - p[0]) * (py - p[1]) - (q[1] - p[1]) * (px - p[0])

    d1, d2, d3 = side(a, b), side(b, c), side(c, a)
    has_negative = d1 < 0 or d2 < 0 or d3 < 0
    has_positive = d1 > 0 or d2 > 0 or d3 > 0
    if not (has_negative and has_positive):
        return True
    for p, q in ((a, b), (b, c), (c, a)):
        length = ((q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2) ** 0.5
        if length and abs(side(p, q)) / length <= 0.5:
            t = ((px - p[0]) * (q[0] - p[0]) + (py - p[1]) * (q[1] - p[1])) / (length * length)
            if -0.01 <= t <= 1.01:
                return True
    return False


@functools.lru_cache(maxsize=None)
def cell_mask(byte: int, flips: int = 0) -> tuple[int, ...]:
    """Return the pixels that a collision byte's shape covers, as 8 rows of 8-bit masks.

    Bit ``x`` of row ``y`` is set when pixel (x, y) of the cell is part of the shape: the block,
    the half block, the platform or the slope's triangle. ``flips`` has bit 0 set for a horizontal
    flip and bit 1 for a vertical flip of the layer tile, and the mask is mirrored to match.

    Renderer#render_collision_tile draws each shape at 16x16 and then halves it to 8x8 with
    ChunkyPNG's nearest-neighbor resize, which keeps source pixel 2i+1. This samples the same
    points of the same 16x16 shapes, and leaves out the arrows that DSVEdit draws on top.
    """
    shape = byte >> 4
    has_top = byte & 1
    bit_1 = byte & 2
    bit_2 = byte & 4
    big = [[False] * 16 for _ in range(16)]

    def rect(x0, y0, x1, y1):
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                big[y][x] = True

    def polygon(points):
        # render_collision_tile draws only convex polygons, so a fan from the first point fills one.
        for i in range(1, len(points) - 1):
            a, b, c = points[0], points[i], points[i + 1]
            for y in range(16):
                for x in range(16):
                    if _in_triangle(x, y, a, b, c):
                        big[y][x] = True

    if shape <= 1:
        if has_top:
            rect(0, 0, 15, 15)
        elif bit_1:
            polygon([(0, 0), (7, 7), (15, 0), (15, 15), (0, 15)])
    elif shape == 2:
        if has_top and bit_1:
            rect(0, 0, 15, 15 if bit_2 else 7)  # a conveyor with top and sides is a full block
        elif has_top:
            rect(0, 0, 15, 7)
        elif bit_1 and not bit_2:
            polygon([(0, 0), (7, 3), (15, 0), (15, 7), (0, 7)])
    elif shape == 3:
        if has_top and bit_1:
            rect(0, 0 if bit_2 else 8, 15, 15)
        elif has_top:
            rect(0, 0 if bit_2 else 8, 15, 7 if bit_2 else 15)
        elif bit_1 and not bit_2:
            polygon([(0, 8), (7, 11), (15, 8), (15, 15), (0, 15)])
    elif shape in (4, 8, 10, 12, 13, 14, 15):
        if shape == 4:
            width, x_offset = 16, 0
        elif shape in (8, 10):
            width, x_offset = 32, (shape - 8) * 8
        else:
            width, x_offset = 64, (shape - 12) * 16
        vertical_flip, horizontal_flip = bit_1, bit_2
        x_end, y_end = (width - 1, 0) if vertical_flip else (0, 15)
        a = (0 - x_offset, 0)
        b = (width - 1 - x_offset, 15)
        c = (x_end - x_offset, y_end)
        for y in range(16):
            for x in range(16):
                if _in_triangle(x, y, a, b, c):
                    big[y][x] = True
        if horizontal_flip:
            big = [row[::-1] for row in big]
    # Shapes 5, 6, 7, 9 and 11 are not valid. DSVEdit draws a placeholder, and AoS has none.

    rows = [[big[2 * y + 1][2 * x + 1] for x in range(8)] for y in range(8)]
    if flips & 1:
        rows = [row[::-1] for row in rows]
    if flips & 2:
        rows = rows[::-1]
    return tuple(sum(1 << x for x in range(8) if row[x]) for row in rows)


class Box(NamedTuple):
    """The part of a room where Soma's feet can be without the game moving him through a door.

    Every field is in room pixels. Soma stays in the room while ``left <= x <= right`` and
    ``top <= y <= bottom``.
    """

    left: int
    top: int
    right: int
    bottom: int


class Collision:
    """A room's collision, as a grid of 8x8-pixel cells read from the ROM.

    ``width`` and ``height`` are the room's size in screens, from its first layer. ``kind(x, y)``
    returns the class of the cell at column ``x`` and row ``y``, and :meth:`solid` and :meth:`floor`
    test single room pixels against the cells' shapes.
    """

    def __init__(self, rom: bytes, room_address: int):
        """Read the collision of the room whose metadata is at ``room_address``.

        Only the room's first layer is read, following Room#read_layer_list_from_rom,
        RoomLayer#read_from_layer_list_entry and BGLayer#read_from_rom. The cells follow
        Renderer#render_collision_tileset, where each layer tile's 16 collision bytes fill a 4x4
        block of cells, row by row. A flipped tile has its block mirrored, as Renderer#render_layer
        mirrors it, and each cell's shape is mirrored with it.

        Raises ValueError when the metadata points outside the ROM, or when it gives a layer
        size outside 1 to 15 screens.
        """
        # Room#read_from_rom: the layer list pointer is the room metadata's third field.
        layer_list = _unpack(rom, room_address + 8, "<I")[0]
        # RoomLayer#read_from_layer_list_entry: the BG layer pointer ends the 12-byte entry.
        metadata = _unpack(rom, layer_list + 8, "<I")[0]
        if metadata == 0:
            # A layer with no metadata is one screen of blank tiles.
            self.width = self.height = 1
            tiles = (0,) * (TILES_PER_SCREEN * TILES_PER_SCREEN)
            tileset = bytes(16)
        else:
            self.width, self.height, _type, _tileset, collision_tileset, tiledata = _unpack(
                rom, metadata, "<BBHIII")
            if not (1 <= self.width <= 15 and 1 <= self.height <= 15):
                raise ValueError(f"invalid layer size {self.width}x{self.height} at {metadata:08X}")
            count = TILES_PER_SCREEN * TILES_PER_SCREEN * self.width * self.height
            tiles = _unpack(rom, tiledata, f"<{count}H")
            # Tileset#read_from_rom_gba puts 16 blank cells in front, so that tile 0 is blank.
            tileset = bytes(16)
            if collision_tileset:
                tileset += _decompress(rom, collision_tileset)

        self.columns = self.width * CELLS_PER_SCREEN
        self.rows = self.height * CELLS_PER_SCREEN
        self._kinds = [[EMPTY] * self.columns for _ in range(self.rows)]
        self._masks = [[(0,) * 8] * self.columns for _ in range(self.rows)]
        tiles_wide = self.width * TILES_PER_SCREEN
        for index, tile in enumerate(tiles):
            tile_x, tile_y = index % tiles_wide, index // tiles_wide
            number = tile & 0x3FFF
            flips = (tile >> 14) & 3
            block = tileset[16 * number:16 * number + 16]
            if len(block) < 16:
                block = bytes(16)  # a tile past the end of the tileset has no collision
            for source, byte in enumerate(block):
                sx, sy = source % CELLS_PER_TILE, source // CELLS_PER_TILE
                dx = CELLS_PER_TILE - 1 - sx if flips & 1 else sx
                dy = CELLS_PER_TILE - 1 - sy if flips & 2 else sy
                x = tile_x * CELLS_PER_TILE + dx
                y = tile_y * CELLS_PER_TILE + dy
                self._kinds[y][x] = classify(byte)
                self._masks[y][x] = cell_mask(byte, flips)

    @property
    def box(self) -> Box:
        """The room's playable box, from the exit rules of the game's sub_08011A44."""
        right = NARROW_RIGHT if self.width == 1 else self.width * SCREEN
        return Box(0, EXIT_MARGIN, right, self.height * SCREEN - EXIT_MARGIN)

    def kind(self, x: int, y: int) -> str:
        """Return the class of the cell at column ``x`` and row ``y``, or EMPTY outside the room."""
        if 0 <= x < self.columns and 0 <= y < self.rows:
            return self._kinds[y][x]
        return EMPTY

    def _covered(self, px: int, py: int, kinds) -> bool:
        x, y = px // CELL, py // CELL
        if not (0 <= x < self.columns and 0 <= y < self.rows) or self._kinds[y][x] not in kinds:
            return False
        return bool(self._masks[y][x][py % CELL] >> (px % CELL) & 1)

    def solid(self, px: int, py: int) -> bool:
        """Return True when the room pixel is part of a solid block or a slope."""
        return self._covered(px, py, _BLOCKING)

    def floor(self, px: int, py: int) -> bool:
        """Return True when the room pixel is part of a solid block, a slope or a platform.

        Pixels in the top and bottom margins never count, because Soma leaves the room before he
        reaches them.
        """
        box = self.box
        return box.top <= py < box.bottom and self._covered(px, py, _FLOOR)

    def floor_top(self, px: int, py: int) -> bool:
        """Return True when the room pixel is a floor pixel with no floor pixel above it."""
        return self.floor(px, py) and not self.floor(px, py - 1)

    def floor_below(self, px: int, start: int, stop: int) -> Optional[int]:
        """Return the first floor top in column ``px``, scanning down from ``start`` to ``stop``."""
        for py in range(start, stop):
            if self.floor_top(px, py):
                return py
        return None

    def catch_below(self, px: int, start: int, stop: int) -> Optional[int]:
        """Return the first floor top that stops Soma as he falls in column ``px``.

        The scan goes down from ``start`` to ``stop``. A floor top in his own column stops him, and
        so does the top of a solid block or a platform within ``FOOT_REACH`` pixels of it on either
        side. A slope counts only in his own column, because his reach over the end of a slope
        has not been measured.
        """
        box = self.box
        beside = [px + k for k in range(-FOOT_REACH, FOOT_REACH + 1) if k]
        for py in range(start, stop):
            if self.floor_top(px, py):
                return py
            if box.top <= py < box.bottom and any(
                    self._covered(x, py, _FLAT) and not self.floor(x, py - 1) for x in beside):
                return py
        return None

    def body_clear(self, px: int, feet_y: int) -> bool:
        """Return True when the column of Soma's body above his feet has no solid pixel in it."""
        return not any(self.solid(px, feet_y - k) for k in range(1, BODY_HEIGHT + 1))


# --- Doors ----------------------------------------------------------------------------------------

def _signed_byte(value: int) -> int:
    return value - 256 if value >= 128 else value


def _signed_word(value: int) -> int:
    return value - 65536 if value >= 32768 else value


def _destination_screen(door: entrance_info.EntranceInfo) -> tuple[int, int]:
    """Return the screen of the neighboring room that the door leads to."""
    return _signed_word(door.dest_x_door) // SCREEN, _signed_word(door.dest_y_door) // SCREEN


def _x_shift(door: entrance_info.EntranceInfo) -> int:
    """Return how far the door moves Soma's x within the screen, at a top or bottom door.

    The game adds ``dest_x_door`` and ``dest_x_offset_door`` to Soma's x within the screen he
    leaves. ``dest_x_door`` also names the destination screen, so the shift is what is left after
    that screen's own x.
    """
    return _signed_word(door.dest_x_door) % SCREEN + _signed_word(door.dest_x_offset_door)


def _door(identifier: str) -> entrance_info.EntranceInfo:
    """Return the entrance_info row for a door identifier, raising KeyError for an unknown one."""
    row = entrance_info.by_door_identifier_unique.get(identifier)
    if row is not None:
        return row
    if identifier in entrance_info.ambiguous_door_identifiers:
        names = sorted(r.door_identifier_unique for r in entrance_info.rows
                       if r.door_identifier_nonunique == identifier)
        raise KeyError(f"{identifier} names {len(names)} doors; use one of {', '.join(names)}")
    raise KeyError(f"no door {identifier} in entrance_info.csv")


class _Crossing(NamedTuple):
    """The two screens on either side of a doorway, and how they line up.

    ``edge`` is the edge of R's screen that Soma crosses on his way in. The place ``t`` pixels
    along that edge faces the place ``t - shift`` pixels along N's matching edge.
    """

    edge: str
    r_screen: tuple[int, int]
    n_screen: tuple[int, int]
    n_address: int
    shift: int


# --- Landing --------------------------------------------------------------------------------------

@dataclass
class _Landing:
    """Where Soma's feet go in room R, and how that spot was chosen."""

    edge: Optional[str] = None
    line: Optional[tuple[int, int, int, int]] = None
    doorway: Optional[tuple[int, int]] = None
    feet_x: int = 0
    feet_y: int = 0
    floor_y: Optional[int] = None
    rule: str = "unplaced"
    warnings: list[str] = field(default_factory=list)


def _runs(flags: list[bool]) -> list[tuple[int, int]]:
    """Return the ``(start, end)`` index ranges where ``flags`` is true, with ``end`` exclusive."""
    runs, start = [], None
    for i, flag in enumerate(flags + [False]):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            runs.append((start, i))
            start = None
    return runs


def _exit_line(collision: Collision, edge: str, screen: tuple[int, int]) -> int:
    """Return the room x or y of the line that Soma crosses at one edge of a screen.

    On an edge of the room, that is the edge of the playable box. A door can also sit between two
    screens of the same room, where the room's layer covers map cells that belong to another
    room. The line is then the boundary between those two screens.
    """
    box = collision.box
    sx, sy = screen
    if edge == "left":
        return max(sx * SCREEN, box.left)
    if edge == "right":
        return min((sx + 1) * SCREEN, box.right)
    if edge == "top":
        return max(sy * SCREEN, box.top)
    return min((sy + 1) * SCREEN, box.bottom)


def _screen_line(collision: Collision, edge: str,
                 screen: tuple[int, int]) -> tuple[int, int, int, int]:
    """Return the exit line at one edge of a screen, as ``(x0, y0, x1, y1)`` in room pixels.

    The line runs for the length of that one screen, cut to the playable box.
    """
    box = collision.box
    sx, sy = screen
    at = _exit_line(collision, edge, screen)
    if edge in ("left", "right"):
        return at, max(sy * SCREEN, box.top), at, min((sy + 1) * SCREEN, box.bottom)
    return max(sx * SCREEN, box.left), at, min((sx + 1) * SCREEN, box.right), at


def _passable(collision: Collision, edge: str, screen: tuple[int, int]) -> list[bool]:
    """Return one flag per pixel along an edge of one screen, true where Soma can cross it.

    The flags come from the row or column of cells just inside the edge's exit line, such as the
    cells at x 232 to 239 for the right edge of a room one screen wide, or at y 48 to 55 for a top
    edge. A cell is passable when it is neither solid nor a slope. A place outside the playable
    box is never passable, which leaves out the top and bottom margins along a side edge.
    """
    box = collision.box
    sx, sy = screen
    line = _exit_line(collision, edge, screen)
    inside = (line if edge in ("left", "top") else line - 1) // CELL
    if edge in ("left", "right"):
        places = range(sy * SCREEN, (sy + 1) * SCREEN)
        return [box.top <= y < box.bottom and collision.kind(inside, y // CELL) not in _BLOCKING
                for y in places]
    places = range(sx * SCREEN, (sx + 1) * SCREEN)
    return [box.left <= x < box.right and collision.kind(x // CELL, inside) not in _BLOCKING
            for x in places]


def _doorway(r_collision: Collision, n_collision: Collision, crossing: _Crossing,
             warnings: list[str]) -> Optional[tuple[int, int]]:
    """Return the doorway along R's edge, in R's pixels with the end exclusive, or None.

    The doorway is where both rooms are open along their shared edge, with the two edges lined up
    as ``crossing`` describes. When the two rooms' openings do not overlap, the doorway is R's own
    opening instead, with a warning. When there are several doorways, the longest wins, and the
    lowest or leftmost breaks a tie.
    """
    r_flags = _passable(r_collision, crossing.edge, crossing.r_screen)
    n_flags = _passable(n_collision, _OPPOSITE[crossing.edge], crossing.n_screen)
    shift = crossing.shift
    both = [r_flags[t] and 0 <= t - shift < SCREEN and n_flags[t - shift] for t in range(SCREEN)]
    runs = _runs(both)
    if not runs:
        runs = _runs(r_flags)
        if not runs:
            return None
        warnings.append("the openings of the two rooms do not line up, so the doorway is R's own")
    side = crossing.edge in ("left", "right")
    chosen = max(runs, key=lambda run: (run[1] - run[0], run[0] if side else -run[0]))
    origin = SCREEN * (crossing.r_screen[1] if side else crossing.r_screen[0])
    others = [run for run in runs if run != chosen and run[1] - run[0] >= 2 * CELL]
    if others:
        spans = ", ".join(f"{origin + a}-{origin + b}" for a, b in others)
        warnings.append(f"other doorways along the edge at {spans}")
    return origin + chosen[0], origin + chosen[1]


def _columns_from_middle(start: int, end: int, low: int, high: int) -> list[int]:
    """Return the columns from ``start`` to ``end - 1`` within ``low`` to ``high``, middle first.

    The rest follow in order of distance from the middle, so the first one that works is the one
    closest to the middle.
    """
    middle = (start + end) // 2
    columns = [x for x in range(start, end) if low <= x <= high]
    return sorted(columns, key=lambda x: (abs(x - middle), x))


def _land_side(collision: Collision, crossing: _Crossing, doorway: tuple[int, int],
               spot: _Landing) -> None:
    """Stand Soma 16 px inside a left or right exit line, on the floor under the doorway's top."""
    box = collision.box
    line = _exit_line(collision, crossing.edge, crossing.r_screen)
    x = line + INSET if crossing.edge == "left" else line - INSET
    top, bottom = doorway
    spot.feet_x = x
    floor = collision.floor_below(x, top, box.bottom)
    if floor is None:
        spot.rule = "side-no-floor"
        spot.feet_y = min(bottom, box.bottom) - 1
        spot.warnings.append("no floor below the doorway, so Soma falls")
        return
    spot.feet_y = spot.floor_y = floor
    if floor > bottom + SIDE_FLOOR_SLACK:
        spot.rule = "side-low-floor"
        spot.warnings.append(f"the floor at y {floor} is {floor - bottom} px below the doorway")
    else:
        spot.rule = "side"


def _land_top(collision: Collision, crossing: _Crossing, doorway: tuple[int, int],
              spot: _Landing) -> None:
    """Put Soma 16 px below the top exit line at the doorway's middle, so that he falls in.

    When solid collision fills that column, the nearest clear column of the doorway is used. The
    floor he falls to is the first one that stops him, which can be a floor that ends just beside
    his column (see :meth:`Collision.catch_below`).
    """
    box = collision.box
    y = _exit_line(collision, "top", crossing.r_screen) + INSET
    columns = _columns_from_middle(*doorway, box.left + INSET, box.right - INSET)
    clear = [x for x in columns if collision.body_clear(x, y) and not collision.solid(x, y)]
    if clear:
        x = clear[0]
    else:
        x = columns[0] if columns else (doorway[0] + doorway[1]) // 2
        spot.warnings.append("no column of the doorway is clear of solid collision")
    spot.feet_x, spot.feet_y = x, y
    spot.floor_y = collision.catch_below(x, y, box.bottom)
    if spot.floor_y is None:
        spot.rule = "top-no-floor"
        spot.warnings.append("no floor below the doorway, so Soma falls through the room")
    else:
        spot.rule = "top"


def _platform_above(collision: Collision, x: int, low: int, high: int) -> Optional[int]:
    """Return the nearest floor top above row ``low`` in column ``x`` that Soma can rise to.

    The scan goes up from ``low`` to ``high``, and only platforms let Soma through from below.
    The scan returns None at the first solid pixel, or when it reaches ``high``.
    """
    for py in range(low, high - 1, -1):
        if collision.solid(x, py):
            return None
        if collision.floor_top(x, py):
            return py
    return None


def _land_bottom(collision: Collision, crossing: _Crossing, doorway: tuple[int, int],
                 spot: _Landing) -> None:
    """Stand Soma on the nearest floor above a bottom doorway, since he came up from below.

    There are two kinds of floor he can reach. One is a platform straight above the doorway,
    which he jumps up through. The other is the top of a ledge 16 px beside the doorway, which he
    can step onto from the shaft when the shaft is open up to that height. The lower of the two
    wins, and the platform wins a tie. Without either, he waits 16 px above the bottom exit line,
    and he falls back out.
    """
    box = collision.box
    low = _exit_line(collision, "bottom", crossing.r_screen) - 1
    high = box.top + INSET
    columns = _columns_from_middle(*doorway, box.left + INSET, box.right - INSET)
    middle = columns[0] if columns else (doorway[0] + doorway[1]) // 2
    # Each candidate is (floor top, 1 for a platform or 0 for a ledge, x, rule).
    candidates = []
    for x in columns:
        floor = _platform_above(collision, x, low, high)
        if floor is not None and collision.body_clear(x, floor):
            candidates.append((floor, 1, x, "bottom"))
            break
    for x in (doorway[0] - INSET, doorway[1] - 1 + INSET):
        if not box.left + INSET <= x <= box.right - INSET:
            continue
        floor = next((py for py in range(low, high - 1, -1) if collision.floor_top(x, py)), None)
        if floor is None or not collision.body_clear(x, floor):
            continue
        # Soma reaches the ledge by rising in the shaft until his feet are level with it.
        if not any(collision.solid(middle, py) for py in range(floor - BODY_HEIGHT, low + 1)):
            candidates.append((floor, 0, x, "bottom-ledge"))
    if candidates:
        floor, _, x, rule = max(candidates, key=lambda c: (c[0], c[1], -c[2]))
        spot.feet_x, spot.feet_y, spot.floor_y, spot.rule = x, floor, floor, rule
        return

    spot.feet_x, spot.feet_y, spot.rule = middle, low + 1 - BOTTOM_INSET, "bottom-no-floor"
    spot.warnings.append("no floor above the doorway, so Soma falls back through it")


def check_landing(collision: Collision, feet_x: int, feet_y: int) -> list[str]:
    """Return the problems with a landing spot, or an empty list when it has none.

    A good spot is inside the playable box, at least 16 px from its sides and its top, and Soma's
    body above his feet is clear of solid collision.
    """
    box = collision.box
    problems = []
    if not box.left + INSET <= feet_x <= box.right - INSET:
        problems.append(f"x {feet_x} is not within {box.left + INSET} to {box.right - INSET}")
    if not box.top + INSET <= feet_y <= box.bottom:
        problems.append(f"y {feet_y} is not within {box.top + INSET} to {box.bottom}")
    if not collision.body_clear(feet_x, feet_y):
        problems.append("Soma's body would be inside solid collision")
    return problems


# --- Camera ---------------------------------------------------------------------------------------

def camera_for(width: int, height: int, feet_x: int, feet_y: int) -> tuple[int, int]:
    """Return the ``(camera_x, camera_y)`` for TeleportPlayer, for Soma's feet at a room pixel.

    ``width`` and ``height`` are the room's size in screens. ``camera_x`` is the room x of the
    screen's left column. ``camera_y`` is the room y of the screen's top row minus 48, because the
    game adds 48 to it.

    Port of Game#start_test_room for AoS. The camera centers on Soma with his feet at screen
    y 111, and then it is clamped to the room. It never comes within 48 px of the room's top or
    bottom, nor within 16 px of the right edge of a room one screen wide.
    """
    room_width = width * SCREEN - (16 if width == 1 else 0)
    room_height = height * SCREEN - 2 * EXIT_MARGIN
    screen_x = feet_x - GBA_WIDTH // 2
    screen_y = feet_y - EXIT_MARGIN - (111 - GBA_HEIGHT // 2) - GBA_HEIGHT // 2
    screen_x = min(max(screen_x, 0), room_width - GBA_WIDTH)
    screen_y = min(max(screen_y, 0), room_height - GBA_HEIGHT)
    return screen_x, screen_y


# --- The castle -----------------------------------------------------------------------------------

class _Castle:
    """One ROM's rooms, with each room's collision read the first time it is needed."""

    def __init__(self, rom: bytes):
        self.rom = rom
        self._collision: dict[int, Collision] = {}

    def collision(self, room_address: int) -> Collision:
        """Return the collision of the room at ``room_address``."""
        if room_address not in self._collision:
            self._collision[room_address] = Collision(self.rom, room_address)
        return self._collision[room_address]

    def _side(self, door: entrance_info.EntranceInfo) -> Optional[tuple[str, tuple[int, int]]]:
        """Return the edge of its own room that a door is on, and the screen of that room it is in.

        Both come from the door's outside cell, which is the map cell just beyond the door, counted
        in screens from its room's top-left. The edge test is a port of Door#direction. Returns
        None when the outside cell is not just beyond an edge. That happens when the door leads
        into a map cell that the room's own layer covers.
        """
        collision = self.collision(door.room_address)
        x, y = _signed_byte(door.x_pos_door), _signed_byte(door.y_pos_door)
        if x == -1:
            edge = "left"
        elif y == -1:
            edge = "top"
        elif x == collision.width:
            edge = "right"
        elif y == collision.height:
            edge = "bottom"
        else:
            return None
        step = _INWARD[edge]
        return edge, (x + step[0], y + step[1])

    def _reverse(self, door: entrance_info.EntranceInfo,
                 warnings: list[str]) -> Optional[entrance_info.EntranceInfo]:
        """Return the door N:R that leads back through the same doorway as R:N, or None.

        When N and R share two doorways, the reverse door is the one that leads to the screen of R
        that R:N is beside, while R:N leads to the screen of N that the reverse door is beside.
        """
        candidates = [row for row in entrance_info.rows
                      if row.room_address == door.dest_room_address
                      and row.dest_room_address == door.room_address]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            warnings.append(f"no reverse door {door.dest_room_identifier}:{door.room_identifier}")
            return None
        mine = self._side(door)
        for candidate in candidates:
            theirs = self._side(candidate)
            if (mine and theirs and _destination_screen(candidate) == mine[1]
                    and _destination_screen(door) == theirs[1]):
                return candidate
        warnings.append(f"none of the {len(candidates)} reverse doors matches"
                        f" {door.door_identifier_unique}")
        return None

    def crossing(self, door: entrance_info.EntranceInfo,
                 warnings: list[str]) -> Optional[_Crossing]:
        """Return the crossing that Soma makes from N into R to reach door R:N, or None.

        The crossing comes from the reverse door N:R when it is on an edge of N. That edge and the
        screen of N beside it are where Soma leaves N, and the reverse door's destination screen is
        where he enters R. Door R:N's own row gives the same crossing from R's side, and it is used
        when the reverse door is missing or does not sit on an edge.

        The screens' edges line up place for place, as the rooms' map positions put them side by
        side. At a top or bottom door the game also shifts Soma's x, by as much as 16 px, and that
        shift is part of the crossing. Side doors carry a y offset too, but the doorways of the two
        rooms line up without it, so it is not used.
        """
        own = self._side(door)
        reverse = self._reverse(door, warnings)
        theirs = self._side(reverse) if reverse is not None else None
        if theirs is not None:
            n_edge, n_screen = theirs
            edge, r_screen = _OPPOSITE[n_edge], _destination_screen(reverse)
            shift = _x_shift(reverse) if edge in ("top", "bottom") else 0
            if own is not None and own != (edge, r_screen):
                warnings.append(f"door {door.door_identifier_unique} and its reverse door"
                                f" {reverse.door_identifier_unique} disagree about the doorway")
        elif own is not None:
            edge, r_screen = own
            n_screen = _destination_screen(door)
            shift = -_x_shift(door) if edge in ("top", "bottom") else 0
        else:
            warnings.append(f"neither {door.door_identifier_unique} nor its reverse door is on an"
                            " edge of its room")
            return None
        return _Crossing(edge, r_screen, n_screen, door.dest_room_address, shift)

    def landing(self, door: entrance_info.EntranceInfo) -> _Landing:
        """Return where Soma's feet go in room R for door R:N, and how that spot was chosen."""
        spot = _Landing()
        r = self.collision(door.room_address)
        box = r.box
        crossing = self.crossing(door, spot.warnings)
        if crossing is not None and not (0 <= crossing.r_screen[0] < r.width
                                         and 0 <= crossing.r_screen[1] < r.height):
            spot.warnings.append(f"screen {crossing.r_screen} is outside room"
                                 f" {door.room_identifier} ({r.width}x{r.height} screens)")
            crossing = None
        if crossing is None:
            spot.feet_x, spot.feet_y = (box.left + box.right) // 2, (box.top + box.bottom) // 2
            return spot

        spot.edge = crossing.edge
        spot.line = _screen_line(r, crossing.edge, crossing.r_screen)
        n = self.collision(crossing.n_address)
        doorway = _doorway(r, n, crossing, spot.warnings)
        if doorway is None:
            # With no opening at all, the doorway is the part of the edge inside the box.
            spot.warnings.append("no opening along the edge")
            sx, sy = crossing.r_screen
            if crossing.edge in ("left", "right"):
                doorway = (max(sy * SCREEN, box.top), min((sy + 1) * SCREEN, box.bottom))
            else:
                doorway = (max(sx * SCREEN, box.left), min((sx + 1) * SCREEN, box.right))
        spot.doorway = doorway
        if crossing.edge in ("left", "right"):
            _land_side(r, crossing, doorway, spot)
        elif crossing.edge == "top":
            _land_top(r, crossing, doorway, spot)
        else:
            _land_bottom(r, crossing, doorway, spot)
        spot.warnings += check_landing(r, spot.feet_x, spot.feet_y)
        return spot

    def teleport_args(self, door: entrance_info.EntranceInfo) -> dict:
        """Return :func:`teleport_args` for one door's row."""
        spot = self.landing(door)
        r = self.collision(door.room_address)
        camera_x, camera_y = camera_for(r.width, r.height, spot.feet_x, spot.feet_y)
        return {
            "room_address": door.room_address,
            "camera_x": camera_x,
            "camera_y": camera_y,
            "soma_x": spot.feet_x - camera_x,
            "soma_y": spot.feet_y - (camera_y + EXIT_MARGIN),
            "feet_x": spot.feet_x,
            "feet_y": spot.feet_y,
            "edge": spot.edge,
            "rule": spot.rule,
            "warnings": spot.warnings,
            "line": list(spot.line) if spot.line else None,
            "doorway": list(spot.doorway) if spot.doorway else None,
            "floor_y": spot.floor_y,
        }


@functools.lru_cache(maxsize=2)
def _castle(rom: bytes) -> _Castle:
    return _Castle(rom)


@functools.lru_cache(maxsize=1)
def _default_rom() -> bytes:
    """Return the US ROM that the annotator's settings name, for a caller that passes no ROM."""
    with open(settings.us_rom_path(), "rb") as handle:
        return handle.read()


# --- The public functions -------------------------------------------------------------------------

def teleport_args(door_identifier: str, rom: Optional[bytes] = None) -> dict:
    """Return the TeleportPlayer arguments that put Soma just inside door ``"R:N"``.

    ``door_identifier`` is a door's unique identifier from entrance_info, such as ``"009:008"`` or
    ``"500:501 (left)"``. ``rom`` is the whole US ROM. When it is None, the ROM is read from the
    path in the annotator's ``rom_path`` setting. Raises KeyError for an identifier that names no
    door, or that names two.

    The result has these keys:

    * ``room_address``: room R's metadata pointer, as an int.
    * ``camera_x`` and ``camera_y``: the camera position to pass to TeleportPlayer. ``camera_x`` is
      the screen's left column in room pixels, and ``camera_y`` is its top row minus 48.
    * ``soma_x`` and ``soma_y``: Soma's feet on the 240x160 screen.
    * ``feet_x`` and ``feet_y``: Soma's feet in room pixels.
    * ``edge``: the edge of R that Soma crosses to reach the door, one of "left", "right", "top" and
      "bottom", or None when the door could not be placed.
    * ``rule``: how the spot was chosen, described below.
    * ``warnings``: problems found along the way, as a list of strings.
    * ``line``: the line that Soma crosses on his way in, as ``[x0, y0, x1, y1]`` in room pixels,
      or None when the door could not be placed. It runs along the edge for the length of the one
      screen that he comes in by.
    * ``doorway``: the doorway's ``[start, end)`` along the edge, in room pixels, or None.
    * ``floor_y``: the floor top that Soma stands on, or falls to from a top door, or None.

    The doorway is the part of R's edge where both R and N are open. The spot depends on the edge:

    * At a left or right edge, Soma stands 16 px inside the exit line, on the first floor at or
      below the doorway's top. The rule is ``side``, or ``side-low-floor`` when that floor lies
      more than 16 px below the doorway, or ``side-no-floor`` when there is no floor at all.
    * At a top edge, Soma came from above, so his feet go 16 px below the top exit line at the
      doorway's middle, and he falls from there. He stops on the first floor below him, or on the
      first flat floor that ends within 5 px of his feet's x. The rule is ``top``, or
      ``top-no-floor`` when no floor lies below.
    * At a bottom edge, Soma came up from below, so he stands on the nearest floor above the
      doorway. That is a platform straight above it (rule ``bottom``), or the top of a ledge just
      beside it (``bottom-ledge``). Without either, he waits 16 px above the bottom exit line
      (``bottom-no-floor``) and falls back out.

    A door that cannot be placed, because neither it nor its reverse door is on an edge of its
    room, gets rule ``unplaced`` and the middle of R's playable box.
    """
    door = _door(door_identifier)
    return _castle(rom if rom is not None else _default_rom()).teleport_args(door)


def all_landings(rom: bytes) -> dict[str, dict]:
    """Return :func:`teleport_args` for every door in entrance_info.csv, by unique identifier."""
    castle = _castle(rom)
    return {row.door_identifier_unique: castle.teleport_args(row) for row in entrance_info.rows}


@functools.lru_cache(maxsize=1)
def _doors_by_room() -> dict[int, list[entrance_info.EntranceInfo]]:
    """Return entrance_info's doors, grouped by the address of the room each one is in."""
    doors: dict[int, list[entrance_info.EntranceInfo]] = {}
    for row in entrance_info.rows:
        doors.setdefault(row.room_address, []).append(row)
    return doors


def _only_through(door: entrance_info.EntranceInfo) -> bool:
    """Return True when the room that a door leads to can be reached only through that door.

    The test follows the castle's doors outwards from the door's own room, without using the door
    itself, and looks for the room that the door leads to.
    """
    reached, queue = {door.room_address}, [door.room_address]
    while queue:
        for row in _doors_by_room().get(queue.pop(), ()):
            if row.door_identifier_unique == door.door_identifier_unique:
                continue
            if row.dest_room_address == door.dest_room_address:
                return False
            if row.dest_room_address not in reached:
                reached.add(row.dest_room_address)
                queue.append(row.dest_room_address)
    return True


def _in_doorway(line: Optional[tuple[int, int, int, int]], x: int, y: int) -> bool:
    """Return True when a room pixel is beside a crossing line, between the line's two ends."""
    if line is None:
        return False
    x0, y0, x1, y1 = line
    if x0 == x1:
        return abs(x - x0) <= DOORWAY_DEPTH and y0 <= y < y1
    return abs(y - y0) <= DOORWAY_DEPTH and x0 <= x < x1


def door_flags(door_identifier: str, rom: Optional[bytes] = None) -> list[tuple[int, int, bool]]:
    """Return the game flags that follow from Soma coming into a room by door ``"R:N"``.

    The arguments are those of :func:`teleport_args`. Each flag is an (address of the flag block,
    flag number, value) triple, and a caller sets them all before it puts Soma at the door's
    landing.

    Two kinds of object in the doorway decide a flag:

    * An outside boss door, which leads to a boss's arena. The arena lets Soma out only once its
      boss is defeated, so the boss's bit of ``emulator.BOSS_FLAGS`` is set. While that bit is
      clear, the door shuts behind Soma and pushes him into the room.
    * A wall that a flag removes. Its flag is set when N can be reached only through this
      doorway, because Soma then passed the wall on his way into N.

    Every other wall of R that a flag removes has its flag cleared, which is its state in a new
    game. So R's walls are the same on every visit by this door, whatever an earlier visit broke.
    """
    door = _door(door_identifier)
    rom = rom if rom is not None else _default_rom()
    line = _castle(rom).landing(door).line
    flags: dict[tuple[int, int], bool] = {}
    for entry in emulator.room_entries(rom, door.room_address):
        if entry.kind != emulator.OBJECT_ENTRY:
            continue
        here = _in_doorway(line, entry.x, entry.y)
        if entry.subtype in emulator.WALLS and entry.var_b:
            key = (emulator.MISC_FLAGS, entry.var_b)
            flags[key] = flags.get(key, False) or (here and _only_through(door))
        elif entry.subtype in emulator.BOSS_DOORS and entry.var_a == 0 and here:
            flags[(emulator.BOSS_FLAGS, entry.var_b)] = True
    return [(address, number, on) for (address, number), on in sorted(flags.items())]


class Exit(NamedTuple):
    """One door out of a room, with the line that Soma crosses to go through it.

    ``line`` is ``(x0, y0, x1, y1)`` in room pixels. It runs along the room's exit line on the edge
    named by ``edge``, for the length of the one screen that the door is beside. ``edge`` and
    ``line`` are None for a door that is not on an edge of its room.
    """

    door: str  # the door's unique identifier
    dest_room_address: int
    edge: Optional[str]
    line: Optional[tuple[int, int, int, int]]


def exits(room_address: int, rom: Optional[bytes] = None) -> list[Exit]:
    """Return every door out of the room at ``room_address``, in entrance_info's order.

    ``rom`` is the whole US ROM, or None to read it as :func:`teleport_args` does.
    """
    castle = _castle(rom if rom is not None else _default_rom())
    found = []
    for door in entrance_info.rows:
        if door.room_address != room_address:
            continue
        side = castle._side(door)
        edge = line = None
        if side is not None:
            edge, screen = side
            line = _screen_line(castle.collision(room_address), edge, screen)
        found.append(Exit(door.door_identifier_unique, door.dest_room_address, edge, line))
    return found
