"""
Custom-pickup icon pipeline for Castlevania: Aria of Sorrow.

Each custom pickup gets a 16x16 4bpp "common item icon": an 0x80-byte blob of four 8x8 tiles,
which the engine DMAs in two halves. Tiles 0 and 1 form the top half, and tiles 2 and 3 the bottom
half. The icon comes from one of two sources:

* ``RomSprite`` extracts the pixel pattern from the **player's own ROM** at build time. It is used
  for art that already exists in the game, such as the metal-gate button. The apworld ships no
  copyrighted ROM pixels. It ships only the ROM address, the frame's pixel region, and a
  palette-index remap, none of which is copyrightable.

* ``ImageFile`` names an **original** PNG (the modder's own art) vendored under ``rom/icons/``. It
  is quantised to the shared items palette (bank 6) at build time. Palettes are not copyrighted, so
  the apworld can ship bank 6's colours itself. They are hardcoded in
  ``custom_pickups.BANK6_PALETTE`` rather than read from the ROM.

Both sources produce a 16x16 grid of **bank-6 palette indices**, which :func:`pack_icon` packs into
the 0x80-byte blob. Index 0 is transparent.

A sprite's GFX is LZ77-compressed (BIOS LZ77UnComp, type 0x10). The decompressed bytes are a run
of 8x8 4bpp tiles that wraps into a page 16 tiles (128 px) wide, so tile *i* occupies page cell
``(i % 16, i // 16)``. A frame's part selects a ``w x h`` pixel region at page offset ``(x, y)``.
These details are verified against the USA ROM.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Tuple

from .tools.png_to_icon import decode_png_rgba, fit_16x16, tile_4bpp, to_indices

GBA_ROM_BASE = 0x08000000
ICON_HALF = 0x40                 # each 16x16 icon = two 0x40-byte halves
_PAGE_TILES_PER_ROW = 16         # 1-dimensional sprite-gfx page width, in 8x8 tiles
_ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")


# --- Icon source descriptors (declared per pickup in the content registry) ---

@dataclass(frozen=True)
class RomSprite:
    """An icon whose pixels are extracted from the player's ROM at build time.

    ``gfx_addr`` is the GBA address of the LZ77-compressed sprite GFX, and ``part`` is the frame's
    ``(x, y, w, h)`` pixel region on the decompressed page. ``remap`` maps each source 4bpp index to
    a bank-6 index. A source index missing from ``remap`` becomes 0, which is transparent.

    With ``crop`` set, the region's opaque content is cropped and centred in 16x16, so the region
    can be any size whose opaque content fits. Without it, the region must be exactly 16x16, and
    any other size raises ValueError."""
    gfx_addr: int
    part: Tuple[int, int, int, int]
    remap: Dict[int, int]
    crop: bool = True

    def __post_init__(self):
        _, _, w, h = self.part
        if not self.crop and (w, h) != (16, 16):
            raise ValueError(f"RomSprite at {self.gfx_addr:#x}: an uncropped region must be "
                             f"16x16, but part {self.part} is {w}x{h}")


@dataclass(frozen=True)
class ImageFile:
    """An original PNG under rom/icons/, quantised to bank 6 at build time.

    The PNG must be 8-bit RGBA and not interlaced. An image that is not 16x16 is cropped to its
    opaque content and centred, so that content must fit in 16x16."""
    filename: str


# --- GBA LZ77 (BIOS LZ77UnComp, compression type 0x10) ---

def lz77_decompress(data: bytes, file_off: int) -> bytes:
    """Decompress a GBA LZ77 (type 0x10) stream starting at ``file_off`` in ``data``."""
    if data[file_off] != 0x10:
        raise ValueError(f"not an LZ77 type-0x10 stream at {file_off:#x} (got {data[file_off]:#x})")
    size = data[file_off + 1] | (data[file_off + 2] << 8) | (data[file_off + 3] << 16)
    out = bytearray()
    p = file_off + 4
    while len(out) < size:
        flags = data[p]; p += 1
        for bit in range(8):
            if len(out) >= size:
                break
            if flags & (0x80 >> bit):
                hi, lo = data[p], data[p + 1]; p += 2
                length = (hi >> 4) + 3
                disp = ((hi & 0x0F) << 8 | lo) + 1
                for _ in range(length):
                    out.append(out[-disp])
            else:
                out.append(data[p]); p += 1
    return bytes(out)


# --- Source-index grids ---

def _sprite_page_pixel(gfx: bytes, x: int, y: int) -> int:
    """4bpp index at page pixel (x, y) for a 1D-laid sprite gfx page (16 tiles/row)."""
    tile = (y // 8) * _PAGE_TILES_PER_ROW + (x // 8)
    off = tile * 32 + (y % 8) * 4 + (x % 8) // 2
    if off >= len(gfx):
        return 0
    byte = gfx[off]
    return byte & 0x0F if x % 2 == 0 else byte >> 4


def rom_sprite_indices(base_rom: bytes, sprite: RomSprite) -> list:
    """Extract the sprite's frame region as a grid of bank-6 indices (via the source-index remap)."""
    gfx = lz77_decompress(base_rom, sprite.gfx_addr - GBA_ROM_BASE)
    x, y, w, h = sprite.part
    grid = [[sprite.remap.get(_sprite_page_pixel(gfx, x + cx, y + cy), 0) for cx in range(w)]
            for cy in range(h)]
    return fit_16x16(grid) if sprite.crop else grid


def image_file_indices(source: ImageFile) -> list:
    """Read the PNG at rom/icons/<filename>, and return it as a 16x16 grid of bank-6 indices.

    Each opaque pixel becomes the index of its nearest bank-6 colour, and each pixel with alpha 0
    becomes index 0. An image that is not 16x16 is cropped to its opaque content and centred.
    Raises ValueError, with the file name in the message, for a PNG that is not 8-bit RGBA, for an
    interlaced PNG, and for an image whose opaque content does not fit in 16x16."""
    from .custom_pickups import BANK6_PALETTE  # local import to avoid a cycle at module load

    with open(os.path.join(_ICON_DIR, source.filename), "rb") as f:
        png = f.read()
    try:
        w, h, px = decode_png_rgba(png)
        grid = to_indices(px, w, h, BANK6_PALETTE)
        return grid if (w, h) == (16, 16) else fit_16x16(grid)
    except ValueError as err:
        raise ValueError(f"icon {source.filename}: {err}") from err


# --- Packing ---

def pack_icon(grid16: list) -> bytes:
    """Pack a 16x16 bank-6-index grid into the 0x80-byte icon blob (top half tiles 0/1, bottom 2/3)."""
    top = tile_4bpp(grid16, 0, 0) + tile_4bpp(grid16, 1, 0)
    bottom = tile_4bpp(grid16, 0, 1) + tile_4bpp(grid16, 1, 1)
    blob = top + bottom
    assert len(blob) == 2 * ICON_HALF
    return blob


def build_icon_tiles(base_rom: bytes, source) -> bytes:
    """Resolve an icon source (RomSprite | ImageFile) to the 0x80-byte bank-6 icon blob."""
    if isinstance(source, RomSprite):
        return pack_icon(rom_sprite_indices(base_rom, source))
    if isinstance(source, ImageFile):
        return pack_icon(image_file_indices(source))
    raise TypeError(f"unknown icon source {source!r}")
