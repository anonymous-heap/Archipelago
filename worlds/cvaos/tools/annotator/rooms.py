"""
Draw an Aria of Sorrow room's background layers as an image, the way DSVEdit draws them.

This is a port of the room renderer in DSVEdit (https://github.com/LagoLunatic/DSVEdit, MIT
License, copyright LagoLunatic), following only its paths for the US release of Castlevania:
Aria of Sorrow on the GBA. Each function names the Ruby methods it comes from.

A room has up to three layers. A layer is a grid of 32x32-pixel tiles, 8x8 of them to each
256x256 screen, and each grid cell names an entry of the layer's tileset. A tileset entry is a 4x4
block of 8x8 minitiles, and each minitile names an 8x8 graphic, a palette and its own flips. The
graphics and palettes are the ones the room loads into VRAM, so a tileset entry has no fixed look
on its own: the room's GFX list and palette list decide it.

:func:`render_room` draws one room, and :func:`room_png` returns that drawing as a PNG, cached on
disk.
"""
from __future__ import annotations

import hashlib
import io
import os
import struct
import tempfile
from dataclasses import dataclass
from functools import cached_property
from typing import Optional

from PIL import Image

ROM_BASE = 0x08000000

# constants/gba_constants.rb
TILE = 32  # TILE_WIDTH and TILE_HEIGHT, in pixels
TILES_PER_SCREEN = 8  # SCREEN_WIDTH_IN_TILES and SCREEN_HEIGHT_IN_TILES
SCREEN = TILE * TILES_PER_SCREEN  # SCREEN_WIDTH_IN_PIXELS
# DSVEdit draws a tileset into an image 16 tiles wide and 32 tall, so a layer tile numbered 512 or
# more falls outside it.
TILESET_TILES = 16 * 32
LAYERS_PER_ROOM = 3  # Room.max_number_of_layers on the GBA
OPAQUE = 0x1F  # RoomLayer#opacity for a layer that is not blended

# Bump this when the drawing changes, so that room_png stops returning the old cached files.
RENDER_VERSION = 1

TRANSPARENT = b"\x00\x00\x00\x00"
RED = b"\xff\x00\x00\xff"  # DSVEdit's colour for missing graphics and missing palette colours
RED_MINITILE = RED * 64
# Renderer#generate_palettes(nil, 16).first: the palette DSVEdit uses when a minitile's is missing.
DUMMY_PALETTE = [TRANSPARENT] + [RED] * 15

# The low and high nibble of each byte, for unpacking 4bpp graphics two pixels per byte.
_LOW_NIBBLE = bytes(value & 0x0F for value in range(256))
_HIGH_NIBBLE = bytes(value >> 4 for value in range(256))


# --- ROM access -----------------------------------------------------------------------------------

def _offset(rom: bytes, address: int, length: int = 1) -> int:
    """Return the file offset of a ROM address, raising ValueError when it is not inside ``rom``.

    Port of GBADummyFilesystem#convert_address, with a bounds check added.
    """
    offset = address - ROM_BASE
    if not 0x08000000 <= address < 0x09000000 or offset + length > len(rom):
        raise ValueError(f"not an address in the ROM: {address:08X}")
    return offset


def _unpack(rom: bytes, address: int, fmt: str) -> tuple:
    return struct.unpack_from(fmt, rom, _offset(rom, address, struct.calcsize(fmt)))


def _u32(rom: bytes, address: int) -> int:
    return _unpack(rom, address, "<I")[0]


def _decompress(rom: bytes, address: int) -> bytes:
    """Return the LZ77 data at ``address``, without the 4-byte header that starts it.

    Port of GBALZ77.decompress followed by GBADummyFilesystem#decompress, which drops the first
    four decompressed bytes. Raises ValueError when the data is not LZ77 or runs off the ROM.
    """
    start = _offset(rom, address, 4)
    header = int.from_bytes(rom[start:start + 4], "little")
    if header & 0xFF != 0x10:
        raise ValueError(f"not LZ77 compressed at {address:08X}: type {header & 0xFF:X}")
    size = header >> 8
    out = bytearray()
    read = start + 4
    try:
        while len(out) < size:
            flags = rom[read]
            read += 1
            for block in range(8):
                if len(out) >= size:
                    break
                if flags & (0x80 >> block):
                    pair = (rom[read] << 8) | rom[read + 1]
                    read += 2
                    count = (pair >> 12) + 3
                    distance = (pair & 0xFFF) + 1
                    if distance > len(out):
                        raise ValueError(f"LZ77 data at {address:08X} refers back past its start")
                    begin = len(out) - distance
                    if distance >= count:
                        out += out[begin:begin + count]
                    else:
                        # The copy overlaps the bytes it writes, so it has to go one byte at a time.
                        for i in range(count):
                            out.append(out[begin + i])
                else:
                    out.append(rom[read])
                    read += 1
    except IndexError:
        raise ValueError(f"LZ77 data at {address:08X} runs past the end of the ROM") from None
    return bytes(out[4:size])


# --- Rooms and layers -----------------------------------------------------------------------------

@dataclass
class _Layer:
    """One entry of a room's layer list, with the BG layer it points to.

    Port of RoomLayer#read_from_layer_list_entry and BGLayer#read_from_rom for AoS (layer.rb).
    ``index`` is the entry's position in the list, and ``width`` and ``height`` are in screens.
    ``tiles`` holds one u16 per 32x32 tile, row by row across the whole layer (LayerTile): bits
    0-13 are the tileset entry, bit 14 is a horizontal flip and bit 15 a vertical flip.
    """

    index: int
    z_index: int
    bg_control: int
    metadata_pointer: int
    width: int = 1
    height: int = 1
    tileset_type: int = 0
    tileset_pointer: int = 0
    tiles: tuple[int, ...] = ()

    @property
    def colors_per_palette(self) -> int:
        """16 or 256, from the colour-mode bit of the BG control (RoomLayer#colors_per_palette)."""
        return 256 if self.bg_control & 0x80 else 16

    @property
    def gfx_base_block(self) -> int:
        """The 16 KB VRAM block the layer's graphics start in (RoomLayer#gfx_base_block)."""
        return (self.bg_control & 0x0C) >> 2


@dataclass
class _GfxPage:
    """One entry of a room's GFX list, with the GfxWrapper it points to (room.rb, gfx_wrapper.rb).

    The page fills ``num_chunks`` 2 KB chunks of VRAM starting at chunk ``load_offset``, from its
    graphics starting at chunk ``first_chunk``.
    """

    rom: bytes
    load_offset: int
    first_chunk: int
    num_chunks: int
    bpp: int
    compressed: bool
    data_pointer: int
    size_in_512_chunks: int

    @property
    def colors_per_palette(self) -> int:
        """16 for 4bpp graphics and 256 for 8bpp (GfxWrapper#colors_per_palette)."""
        return 16 if self.bpp == 4 else 256

    @cached_property
    def data(self) -> bytes:
        """The page's graphics, decompressed if they are stored compressed (GfxWrapper#gfx_data)."""
        if self.compressed:
            return _decompress(self.rom, self.data_pointer)
        length = 512 * self.size_in_512_chunks
        start = _offset(self.rom, self.data_pointer, length)
        return self.rom[start:start + length]

    @cached_property
    def nibbles(self) -> bytes:
        """The graphics as 4bpp pixels, one byte each: the low nibble of each byte comes first."""
        out = bytearray(2 * len(self.data))
        out[0::2] = self.data.translate(_LOW_NIBBLE)
        out[1::2] = self.data.translate(_HIGH_NIBBLE)
        return bytes(out)

    def minitile(self, number: int, four_bpp: bool) -> Optional[bytes]:
        """Return the 64 pixel values of the page's minitile ``number``, row by row.

        Port of the reading half of Renderer#render_1_dimensional_minitile. Minitiles are stored
        one after another, 32 bytes each at 4bpp and 64 at 8bpp. Returns None when the minitile
        lies past the end of the graphics, where DSVEdit's GfxWrapper#read_from_data raises.
        """
        size = 32 if four_bpp else 64
        offset = size * number
        if offset + size > len(self.data):
            return None
        if four_bpp:
            return self.nibbles[2 * offset:2 * offset + 64]
        return self.data[offset:offset + 64]


@dataclass
class _PalettePage:
    """One entry of a room's palette list (PaletteWrapper#read).

    The room loads ``num_palettes`` palettes from the list at ``list_pointer``, starting at its
    palette ``palette_index``, into the palette slots starting at ``load_offset``.
    """

    list_pointer: int
    load_offset: int
    palette_index: int
    num_palettes: int


@dataclass
class _Room:
    """What drawing needs from a room's metadata (Room#read_from_rom, the "aos" branch)."""

    layers: list[_Layer]
    gfx_pages: list[_GfxPage]
    palette_pages: list[_PalettePage]
    color_effects: int

    @property
    def width(self) -> int:
        """Width in screens, which is the first layer's width (Room#main_layer_width)."""
        return self.layers[0].width

    @property
    def height(self) -> int:
        """Height in screens, which is the first layer's height (Room#main_layer_height)."""
        return self.layers[0].height

    def opacity(self, layer: _Layer) -> int:
        """Return a layer's 5-bit opacity, 0x1F for opaque (RoomLayer#opacity, the "aos" branch).

        A layer is half transparent (0x0F) when the room's colour effect is alpha blending and the
        layer is one of the blend's first targets. Bits 6 and 7 of ``color_effects`` give the effect
        type, and bits 1 to 3 mark layers 0 to 2 as first targets.
        """
        if self.color_effects & 0xC0 == 0x40 and self.color_effects & (1 << (layer.index + 1)):
            return 0x0F
        return OPAQUE


def _read_layer(rom: bytes, index: int, entry_address: int) -> _Layer:
    """Port of RoomLayer#read_from_layer_list_entry and BGLayer#read_from_rom for AoS."""
    z_index, _scroll_mode, bg_control, _width_px, _height_px, metadata = _unpack(
        rom, entry_address, "<BBHHHI")
    layer = _Layer(index, z_index, bg_control, metadata)
    if metadata == 0:
        # An empty layer is one screen of blank tiles.
        layer.tiles = (0,) * (TILES_PER_SCREEN * TILES_PER_SCREEN)
        return layer
    (layer.width, layer.height, layer.tileset_type, layer.tileset_pointer, _collision_tileset,
     tiledata) = _unpack(rom, metadata, "<BBHIII")
    if layer.width > 15 or layer.height > 15:
        raise ValueError(f"invalid layer size {layer.width}x{layer.height} at {metadata:08X}")
    count = TILES_PER_SCREEN * TILES_PER_SCREEN * layer.width * layer.height
    layer.tiles = _unpack(rom, tiledata, f"<{count}H")
    return layer


def _read_gfx_pages(rom: bytes, gfx_list_pointer: int) -> list[_GfxPage]:
    """Port of RoomGfxPage.from_room_gfx_page_list and GfxWrapper#initialize for AoS.

    The list is 8-byte entries ending at a null pointer. Each entry points to a GFX wrapper, whose
    first four bytes give the data type (0 for raw graphics after the header, 1 for a pointer to
    LZ77 graphics), the bits per pixel and the raw size in 512-byte units.
    """
    pages = []
    address = gfx_list_pointer
    while True:
        gfx_pointer, load_offset, first_chunk, num_chunks, _unknown = _unpack(
            rom, address, "<IBBBB")
        if gfx_pointer == 0:
            return pages
        data_type, bpp, _unknown_3, size_in_512_chunks = _unpack(rom, gfx_pointer, "<BBBB")
        if bpp not in (4, 8):
            raise ValueError(f"unknown bpp {bpp} in the GFX wrapper at {gfx_pointer:08X}")
        if data_type not in (0, 1):
            raise ValueError(f"unknown GFX data type {data_type:02X} at {gfx_pointer:08X}")
        compressed = data_type == 1
        data_pointer = _u32(rom, gfx_pointer + 4) if compressed else gfx_pointer + 4
        pages.append(_GfxPage(rom, load_offset, first_chunk, num_chunks, bpp, compressed,
                              data_pointer, size_in_512_chunks))
        address += 8


def _read_palette_pages(rom: bytes, palette_wrapper_pointer: int) -> list[_PalettePage]:
    """Port of PaletteWrapper.from_palette_wrapper_pointer: 8-byte entries up to a null pointer."""
    pages = []
    address = palette_wrapper_pointer
    while True:
        list_pointer, load_offset, palette_index, num_palettes, _unknown = _unpack(
            rom, address, "<IBBBB")
        if list_pointer == 0:
            return pages
        pages.append(_PalettePage(list_pointer, load_offset, palette_index, num_palettes))
        address += 8


def _read_room(rom: bytes, room_address: int) -> _Room:
    """Read the parts of the room at ``room_address`` that drawing needs.

    Port of Room#read_from_rom for AoS, with Room#read_layer_list_from_rom,
    Room#read_graphic_tilesets_from_rom, Room#read_palette_pages_from_rom and the "aos" branch of
    Room#read_extra_data_from_rom. A room always has three entries in its layer list.
    """
    (_lcd_control, _swap_flag, _alternate, layer_list, gfx_list, palette_wrapper, _entities,
     _doors, extra, _extra_2) = _unpack(rom, room_address, "<HHIIIIIIII")
    layers = [_read_layer(rom, i, layer_list + 12 * i) for i in range(LAYERS_PER_ROOM)]
    return _Room(layers, _read_gfx_pages(rom, gfx_list), _read_palette_pages(rom, palette_wrapper),
                 color_effects=(extra >> 16) & 0xFFFF)


# --- Palettes -------------------------------------------------------------------------------------

def _color(value: int) -> bytes:
    """Return a GBA colour as RGBA bytes (Renderer.read_16_bit_rgb_color).

    The colour is 15 bits, xbbbbbgggggrrrrr. Each 5-bit channel becomes 8 bits by repeating its top
    three bits below it, so 31 becomes 255.
    """
    channels = (value & 0x1F, (value >> 5) & 0x1F, (value >> 10) & 0x1F)
    return bytes([(c << 3) | (c >> 2) for c in channels] + [255])


def _generate_palettes(rom: bytes, list_pointer: int, colors_per_palette: int) -> list[list[bytes]]:
    """Return every palette of a palette list, as lists of RGBA colours.

    Port of Renderer#generate_palettes and Renderer#read_single_palette. The list starts with four
    bytes: a zero, an unknown byte, the number of 16-colour rows, and another zero. Palette ``i``
    starts at row ``i``, also for 256 colours, since AoS's COLOR_OFFSETS_PER_256_PALETTE_INDEX is
    16. A palette that would run past the last row is cut there and filled up with red.

    Colour 0 of each palette is replaced with transparent here, because a pixel of value 0 is
    always transparent when DSVEdit draws it.
    """
    zero_1, _unknown, rows, zero_2 = _unpack(rom, list_pointer, "<BBBB")
    if zero_1 or zero_2:
        raise ValueError(f"Invalid palette list pointer: {list_pointer:08X}")
    start = list_pointer + 4
    end = start + rows * 2 * 16
    palettes = []
    for i in range(rows):
        address = start + 2 * 16 * i
        count = min(colors_per_palette, (end - address) // 2)
        colors = [_color(value) for value in _unpack(rom, address, f"<{count}H")]
        colors += [RED] * (colors_per_palette - count)
        colors[0] = TRANSPARENT
        palettes.append(colors)
    return palettes


def _ruby_slice(values: list, start: int, length: int) -> list:
    """Return what Ruby's ``values[start, length]`` gives as the right side of an assignment.

    Ruby returns nil when ``start`` is past the end, and assigning nil inserts one nil element.
    """
    if start > len(values):
        return [None]
    return values[start:start + length]


def _ruby_splice(target: list, start: int, length: int, values: list) -> None:
    """Do what Ruby's ``target[start, length] = values`` does.

    Ruby pads the array with nil up to ``start``, and the array grows or shrinks when ``values``
    has more or fewer than ``length`` elements. Python's slice assignment does the second part but
    not the first.
    """
    if start > len(target):
        target.extend([None] * (start - len(target)))
    target[start:start + length] = values


# --- Tilesets -------------------------------------------------------------------------------------

def _read_tileset(rom: bytes, pointer: int, tileset_type: int) -> list[list[tuple]]:
    """Return a tileset's entries, each a list of minitiles, with a blank entry 0 in front.

    Port of Tileset#read_from_rom_gba, GBATilesetTile, GBA256TilesetTile, MiniTile and MiniTile256.
    Bit 1 of ``tileset_type`` means the tileset is LZ77 compressed. Otherwise only type 1 is known,
    0x1000 raw bytes, and DSVEdit draws nothing from any other type.

    Bit 0 of ``tileset_type`` means 256 colours. Those entries are 16 bytes, one per minitile, each
    the minitile's number with no flips and palette 0. Other entries are 32 bytes, one u16 per
    minitile: bits 0-7 are the number within a page, bits 8-9 the page, bit 10 a horizontal flip,
    bit 11 a vertical flip and bits 12-15 the palette. Each minitile here is the tuple
    ``(number, page, horizontal_flip, vertical_flip, palette)``.
    """
    if tileset_type & 2:
        data = _decompress(rom, pointer)
    elif tileset_type == 1:
        start = _offset(rom, pointer)
        data = rom[start:start + 0x1000]
    else:
        return []
    wide = tileset_type & 1
    size = 16 if wide else 32

    def entry(chunk: bytes) -> list[tuple]:
        if wide:
            return [(value, 0, False, False, 0) for value in chunk]
        values = struct.unpack(f"<{len(chunk) // 2}H", chunk[:len(chunk) // 2 * 2])
        return [(v & 0xFF, (v >> 8) & 3, bool(v & 0x400), bool(v & 0x800), v >> 12) for v in values]

    tiles = [entry(bytes(size))]
    offset = 0
    while True:
        tiles.append(entry(data[offset:offset + size]))
        offset += size
        if offset >= len(data):
            return tiles


class _Tileset:
    """A layer's tileset, drawn one entry at a time with its room's graphics and palettes.

    Port of the GBA branch of Renderer#render_tileset, which sets up the graphics and palettes,
    and of Renderer#render_tileset_gba, which draws each entry. DSVEdit draws the whole tileset
    into one image, while this draws only the entries a layer uses and keeps them.
    """

    def __init__(self, rom: bytes, room: _Room, layer: _Layer, palette_cache: dict):
        self.tiles = _read_tileset(rom, layer.tileset_pointer, layer.tileset_type)
        self.gfx_pages = room.gfx_pages
        self.base_block = layer.gfx_base_block

        # VRAM is split into 2 KB chunks. Each GFX page fills a run of them, and a chunk maps to
        # the page that fills it and the chunk of that page's graphics it holds.
        self.chunks: dict[int, tuple[int, int]] = {}
        for page_index, page in enumerate(room.gfx_pages):
            for i in range(page.num_chunks):
                self.chunks[page.load_offset + i] = (page_index, page.first_chunk + i)

        def palettes_for(page: _PalettePage, colors: int) -> list:
            key = (page.list_pointer, colors)
            if key not in palette_cache:
                palette_cache[key] = _generate_palettes(rom, page.list_pointer, colors)
            return palette_cache[key]

        # The palette slots, filled from each palette page the way DSVEdit fills them. The 16-colour
        # list uses the layer's colour mode, as DSVEdit does, so a 256-colour layer gets 256-colour
        # palettes in both lists.
        self.palettes: list = []
        self.palettes_256: list = []
        any_256 = any(page.colors_per_palette == 256 for page in room.gfx_pages)
        for page in room.palette_pages:
            loaded = palettes_for(page, layer.colors_per_palette)
            _ruby_splice(self.palettes, page.load_offset, page.num_palettes,
                         _ruby_slice(loaded, page.palette_index, page.num_palettes))
            if any_256:
                loaded = palettes_for(page, 256)
                _ruby_splice(self.palettes_256, page.load_offset, page.num_palettes,
                             _ruby_slice(loaded, page.palette_index, page.num_palettes))

        self._minitiles: dict[tuple, bytes] = {}
        self._entries: dict[int, Optional[Image.Image]] = {}

    def _minitile(self, minitile: tuple) -> bytes:
        """Return one minitile as 8x8 RGBA pixels, with its flips applied.

        Port of the loop body of Renderer#render_tileset_gba and of
        Renderer#render_1_dimensional_minitile. The VRAM chunk is the layer's base block (8
        chunks each), plus 4 chunks per page, plus bits 6-7 of the number. A minitile whose page
        or chunk holds no graphics is drawn red, as DSVEdit does. A minitile past the end of its
        graphics is drawn red too, where DSVEdit raises an error instead. No AoS room has one.

        The palette's length decides how the graphics are read: 16 colours means 4bpp and 256
        means 8bpp. A minitile with no palette in its slot gets DSVEdit's dummy palette, which is
        red apart from the transparent colour 0.
        """
        if minitile in self._minitiles:
            return self._minitiles[minitile]
        number, page_number, horizontal_flip, vertical_flip, palette_index = minitile
        chunk = self.chunks.get(self.base_block * 8 + page_number * 4 + ((number & 0xC0) >> 6))
        if page_number >= len(self.gfx_pages) or chunk is None:
            pixels = RED_MINITILE
        else:
            page_index, chunk_offset = chunk
            page = self.gfx_pages[page_index]
            palettes = self.palettes if page.colors_per_palette == 16 else self.palettes_256
            palette = palettes[palette_index] if palette_index < len(palettes) else None
            palette = palette or DUMMY_PALETTE
            values = page.minitile((number & 0x3F) + chunk_offset * 0x40, len(palette) == 16)
            if values is None:
                pixels = RED_MINITILE
            else:
                rows = [values[8 * y:8 * y + 8] for y in range(8)]
                if horizontal_flip:
                    rows = [row[::-1] for row in rows]
                if vertical_flip:
                    rows.reverse()
                pixels = b"".join(map(palette.__getitem__, b"".join(rows)))
        self._minitiles[minitile] = pixels
        return pixels

    def entry(self, index: int) -> Optional[Image.Image]:
        """Return tileset entry ``index`` as a 32x32 RGBA image, or None when nothing in it shows.

        The 16 minitiles fill the entry left to right, then top to bottom. An entry the tileset
        does not have is blank, as it is in DSVEdit's tileset image. An entry number past that
        image is a red square, which is what DSVEdit's room view (LayerItem#render_layer) shows.
        """
        if index in self._entries:
            return self._entries[index]
        if index >= TILESET_TILES:
            image = Image.new("RGBA", (TILE, TILE), (255, 0, 0, 255))
        elif index >= len(self.tiles):
            image = None
        else:
            pixels = bytearray(TILE * TILE * 4)
            for n, minitile in enumerate(self.tiles[index]):
                x, y = 8 * (n % 4), 8 * (n // 4)
                drawn = self._minitile(minitile)
                for row in range(8):
                    start = ((y + row) * TILE + x) * 4
                    pixels[start:start + 32] = drawn[32 * row:32 * row + 32]
            image = None
            if any(pixels[3::4]):
                image = Image.frombytes("RGBA", (TILE, TILE), bytes(pixels))
        self._entries[index] = image
        return image


# --- Drawing --------------------------------------------------------------------------------------

def _render_layer(layer: _Layer, tileset: _Tileset) -> Image.Image:
    """Draw a layer at its own size, with transparent pixels wherever its tiles are transparent.

    Port of Renderer#render_layer. A tile with the horizontal flip bit is mirrored left to right,
    and one with the vertical flip bit top to bottom, after the minitiles' own flips.
    """
    image = Image.new("RGBA", (layer.width * SCREEN, layer.height * SCREEN), (0, 0, 0, 0))
    columns = layer.width * TILES_PER_SCREEN
    drawn: dict[int, Optional[Image.Image]] = {}
    for n, value in enumerate(layer.tiles):
        if value not in drawn:
            tile = tileset.entry(value & 0x3FFF)
            if tile is not None and value & 0x4000:
                tile = tile.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            if tile is not None and value & 0x8000:
                tile = tile.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            drawn[value] = tile
        tile = drawn[value]
        if tile is not None:
            image.paste(tile, ((n % columns) * TILE, (n // columns) * TILE))
    return image


def render_room(rom: bytes, room_address: int) -> Image.Image:
    """Return the room at ``room_address`` as an RGBA image of its background layers.

    ``rom`` is the whole US ROM, and ``room_address`` is the room's metadata pointer, such as
    0x0850EF9C for room 000. The image has one pixel per room pixel, and it is the room's size in
    screens times 256 in each direction. The room's size is its first layer's size.

    This follows Renderer#render_room and Renderer#render_room_layer, and DSVEdit's room view
    (MainWindow#load_layers and LayerItem) where they differ. The layers are drawn in order of
    decreasing z index (Room#z_ordered_layers), so the layer with the lowest z index ends up on
    top. A layer with no metadata or no tileset is left out, as the room view leaves it out. Each
    layer is drawn from its top-left corner at its own size, and a layer larger than the room is
    cut off at the room's edge.

    When the room's colour effect is alpha blending, the layers it blends are drawn half
    transparent: their visible pixels get alpha 123, which is DSVEdit's opacity of 0x0F out of
    0x1F widened to 8 bits.

    Pixel value 0 of every minitile is transparent, and so is any place where no layer has a
    visible pixel. DSVEdit fills those places with black, while the game shows its backdrop
    colour there. A minitile on a graphics page that the room does not load is a red square,
    which is how DSVEdit marks missing graphics. The collision layer is not drawn, since DSVEdit
    shows it only as an overlay that starts hidden.

    Like DSVEdit, this draws each layer as stored. It does not show animated tiles or palette
    animation, and it does not show how a layer scrolls, repeats or rotates in the game. Raises
    ValueError when the room's data points outside the ROM or is malformed.
    """
    room = _read_room(rom, room_address)
    width, height = room.width * SCREEN, room.height * SCREEN
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    tilesets: dict[tuple, _Tileset] = {}
    palette_cache: dict = {}
    # Ruby's sort_by is not stable, but no AoS room has two layers with the same z index.
    for layer in sorted(room.layers, key=lambda layer: -layer.z_index):
        if layer.metadata_pointer == 0 or layer.tileset_pointer == 0:
            continue
        # DSVEdit keeps one tileset image per tileset, colour mode and base block in a room.
        key = (layer.tileset_pointer, layer.tileset_type, layer.colors_per_palette,
               layer.gfx_base_block)
        if key not in tilesets:
            tilesets[key] = _Tileset(rom, room, layer, palette_cache)
        image = _render_layer(layer, tilesets[key])
        opacity = room.opacity(layer)
        if opacity != OPAQUE:
            alpha = (opacity << 3) | (opacity >> 2)
            image.putalpha(image.getchannel("A").point(lambda a, alpha=alpha: min(a, alpha)))
        if image.size != canvas.size:
            image = image.crop((0, 0, width, height))
        canvas.alpha_composite(image)
    return canvas


# --- PNG cache ------------------------------------------------------------------------------------

_ROM_MD5: dict[tuple[str, int, int], str] = {}


def _rom_md5(rom_path: str) -> tuple[str, Optional[bytes]]:
    """Return the ROM's md5, and its contents when this call had to read them.

    The md5 is kept for each path, size and modification time, so that a cached PNG can be found
    without reading the ROM again.
    """
    status = os.stat(rom_path)
    key = (os.path.abspath(rom_path), status.st_size, status.st_mtime_ns)
    if key in _ROM_MD5:
        return _ROM_MD5[key], None
    with open(rom_path, "rb") as handle:
        rom = handle.read()
    _ROM_MD5[key] = hashlib.md5(rom).hexdigest()
    return _ROM_MD5[key], rom


def room_png(rom_path: str, room_address: int, cache_dir: str) -> bytes:
    """Return :func:`render_room` of the room at ``room_address`` as PNG bytes, cached on disk.

    The PNG is stored in ``cache_dir`` as ``room-v<RENDER_VERSION>-<ROM md5>-<address>.png``, and
    a later call for the same ROM and room returns that file's contents without drawing. The
    folder is created when it is missing. The file is written under a temporary name and then
    renamed, so that a caller in another thread never reads half of it.
    """
    md5, rom = _rom_md5(rom_path)
    path = os.path.join(cache_dir, f"room-v{RENDER_VERSION}-{md5}-{room_address:08X}.png")
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        pass
    if rom is None:
        with open(rom_path, "rb") as handle:
            rom = handle.read()
    buffer = io.BytesIO()
    render_room(rom, room_address).save(buffer, "PNG")
    png = buffer.getvalue()
    os.makedirs(cache_dir, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as out:
            out.write(png)
        os.replace(temporary, path)
    except PermissionError:
        # On Windows the rename fails while another thread reads a copy that thread just wrote.
        if not os.path.exists(path):
            raise
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return png
