"""
Custom-pickup icon pipeline for Castlevania: Aria of Sorrow.

Produces a 16x16 4bpp "common item icon" (the 0x80-byte blob the engine DMAs: top half tiles 0/1,
bottom half tiles 2/3) for a custom pickup, from one of two sources:

* ``RomSprite`` -- extract the pixel pattern from the **player's own ROM** at build time. Used for art
  that already exists in the game (e.g. the metal-gate-button). The apworld ships NO copyrighted ROM
  pixels -- only the (non-copyrightable) ROM address, the frame's tile layout, and a palette-index
  remap. The raw 4bpp indices (the art) come from the player's ROM each build.

* ``ImageFile`` -- a vendored **original** PNG (the modder's own art, under ``rom/icons/``), quantised
  to the shared items palette (bank 6) at build time. (Palettes are not copyrighted, so bank 6's
  colours may be read from the ROM or hardcoded; we hardcode them in custom_pickups.BANK6_PALETTE.)

Both paths end as a 16x16 grid of **bank-6 palette indices**, packed by :func:`pack_icon` into the
0x80-byte blob. Index 0 is transparent.

ROM-sprite extraction details (all verified against the USA ROM): a sprite's GFX is LZ77-compressed
(BIOS LZ77UnComp, type 0x10); the decompressed bytes are 8x8 4bpp tiles laid out "1-dimensionally"
into a page 16 tiles (128 px) wide, i.e. minitile *i* occupies page cell ``(i % 16, i // 16)``. A
frame's part selects a ``w x h`` pixel region at page offset ``(x, y)``.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

GBA_ROM_BASE = 0x08000000
ICON_HALF = 0x40                 # each 16x16 icon = two 0x40-byte halves
_PAGE_TILES_PER_ROW = 16         # 1-dimensional sprite-gfx page width, in 8x8 tiles


# --- Icon source descriptors (declared per pickup in the content registry) ---

@dataclass(frozen=True)
class RomSprite:
    """A 16x16 icon extracted from the player's ROM at build time. ``gfx_addr`` is the GBA address of
    the (LZ77-compressed) sprite GFX; ``part`` = (x, y, w, h) pixel region of the 1D-laid page for the
    frame to use; ``remap`` maps each source 4bpp index -> a bank-6 index (0 = transparent). ``crop``
    re-centres the opaque content into 16x16 (to match a clean icon framing)."""
    gfx_addr: int
    part: Tuple[int, int, int, int]
    remap: Dict[int, int]
    crop: bool = True


@dataclass(frozen=True)
class ImageFile:
    """An original 16x16 (or smaller, auto-fit) PNG under rom/icons/, quantised to bank 6 at build."""
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
    return _fit_16x16(grid) if sprite.crop else grid


# --- 16x16 fit + pack ---

def _fit_16x16(grid: list) -> list:
    """Crop the opaque (nonzero) bounding box and centre it in a 16x16 index grid."""
    opaque = [(x, y) for y in range(len(grid)) for x in range(len(grid[0])) if grid[y][x] != 0]
    if not opaque:
        raise ValueError("icon source is fully transparent")
    x0 = min(x for x, _ in opaque); x1 = max(x for x, _ in opaque)
    y0 = min(y for _, y in opaque); y1 = max(y for _, y in opaque)
    cw, ch = x1 - x0 + 1, y1 - y0 + 1
    if cw > 16 or ch > 16:
        raise ValueError(f"opaque content is {cw}x{ch}; must be <= 16x16")
    ox, oy = (16 - cw) // 2, (16 - ch) // 2
    out = [[0] * 16 for _ in range(16)]
    for yy in range(ch):
        for xx in range(cw):
            out[oy + yy][ox + xx] = grid[y0 + yy][x0 + xx]
    return out


def _tile_4bpp(grid: list, tx: int, ty: int) -> bytes:
    """One 8x8 4bpp tile at tile-column tx, tile-row ty (low nibble = left pixel)."""
    out = bytearray()
    for row in range(8):
        for col in range(0, 8, 2):
            lo = grid[ty * 8 + row][tx * 8 + col]
            hi = grid[ty * 8 + row][tx * 8 + col + 1]
            out.append((hi << 4) | lo)
    return bytes(out)


def pack_icon(grid16: list) -> bytes:
    """Pack a 16x16 bank-6-index grid into the 0x80-byte icon blob (top half tiles 0/1, bottom 2/3)."""
    top = _tile_4bpp(grid16, 0, 0) + _tile_4bpp(grid16, 1, 0)
    bottom = _tile_4bpp(grid16, 0, 1) + _tile_4bpp(grid16, 1, 1)
    blob = top + bottom
    assert len(blob) == 2 * ICON_HALF
    return blob


def build_icon_tiles(base_rom: bytes, source) -> bytes:
    """Resolve an icon source (RomSprite | ImageFile) to the 0x80-byte bank-6 icon blob."""
    if isinstance(source, RomSprite):
        return pack_icon(rom_sprite_indices(base_rom, source))
    if isinstance(source, ImageFile):
        return _image_file_tiles(source)
    raise TypeError(f"unknown icon source {source!r}")


def _image_file_tiles(source: ImageFile) -> bytes:
    """Quantise a vendored original PNG (rom/icons/<filename>) to bank 6 -> the icon blob."""
    import os
    from .custom_pickups import BANK6_PALETTE  # local import to avoid a cycle at module load
    from .tools.png_to_icon import decode_png_rgba, fit_16x16, bgr555_to_rgb

    path = os.path.join(os.path.dirname(__file__), "icons", source.filename)
    w, h, px = decode_png_rgba(path)
    if (w, h) != (16, 16):
        w, h, px = fit_16x16(w, h, px)
    pal_rgb = [bgr555_to_rgb(c) for c in BANK6_PALETTE]

    def nearest(r, g, b):
        best, bi = 1 << 30, 1
        for i in range(1, 16):                      # never map an opaque pixel to index 0 (transparent)
            pr, pg, pb = pal_rgb[i]
            d = (pr - r) ** 2 + (pg - g) ** 2 + (pb - b) ** 2
            if d < best:
                best, bi = d, i
        return bi

    grid = [[0 if px[y * 16 + x][3] == 0 else nearest(*px[y * 16 + x][:3]) for x in range(16)]
            for y in range(16)]
    return pack_icon(grid)
