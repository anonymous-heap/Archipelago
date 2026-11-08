#!/usr/bin/env python3
"""
Convert an RGBA PNG into an AoS "common item icon", and print its bytes as Python source.

The icon is four 4bpp GBA tiles, with each pixel mapped to the nearest color of a 16-color OBJ
palette. The engine loads a pickup icon on demand with two DMAs: 0x40 bytes for the top half
(tiles 0 and 1) from sheet_base+off, and 0x40 bytes for the bottom half (tiles 2 and 3) from
sheet_base+off+0x200. This tool prints the two halves together as one 0x80-byte blob. A source
image that is not 16x16 is cropped to its opaque content and centered, so that content must fit in
16x16.

rom/icon.py also imports this module's PNG decoder, quantizer, 16x16 fit and tile packer to build
icons, so this module is imported during a build, not only run by hand. Those functions raise
ValueError for unusable input, and the command-line entry point turns that error into an exit
message.

Palette: floor pickups all render through OBJ palette bank 6, the shared items palette. Because it
is loaded for every pickup in every room, it holds the same colors wherever a pickup appears. Bank
6 is items-palette sub-palette 2, and its 16 colors are static in the ROM at
0x082099FC + 4 + 2*0x20 = 0x08209A40. They form a rainbow that includes gold #F8D848, tan #F8B070,
and greys #A0A0A8 and #E8D8D0. Pass them as BGR555 hwords with --palette. ``BANK6_PALETTE`` in
rom/custom_pickups.py holds the same values. Index 0 is transparent.

Without --palette, the tool derives a palette from the image's own colors. That output has the
right tile structure, but its in-game colors are wrong until the tool is rerun with --palette.

Nearest-color matching can map a mid-tone to a different hue, so dark brass may come out brown or
red. For a deliberate look, recolor the source to bank-6 colors before converting it.

Usage:
  python png_to_icon.py button_pickup.png                       # provisional (image's own colors)
  python png_to_icon.py button_pickup.png --palette "0,0x7fff,..."   # final (bank 6 colors)
"""
from __future__ import annotations

import argparse
import struct
import sys
import zlib


def decode_png_rgba(data: bytes) -> tuple[int, int, list[tuple[int, int, int, int]]]:
    """Decode the bytes of a PNG file into its width, height and row-major RGBA pixels.

    Only non-interlaced 8-bit RGBA images are supported. Raises ValueError when ``data`` is not a
    PNG, when the image is not 8-bit RGBA, and when the image is interlaced. The rows of an
    interlaced image are stored in a different order, so decoding them as plain rows would return
    the wrong pixels."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file (the 8-byte PNG signature is missing)")
    pos = 8
    w = h = bd = ct = interlace = None
    idat = b""
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            # width, height, bit depth, color type, compression, filter method, interlace method
            w, h, bd, ct, _, _, interlace = struct.unpack(">IIBBBBB", chunk[:13])
        elif typ == b"IDAT":
            idat += chunk
        elif typ == b"IEND":
            break
        pos += 12 + ln
    if ct != 6 or bd != 8:
        raise ValueError(f"expected an 8-bit RGBA PNG (colour type 6, bit depth 8), "
                         f"got colour type {ct} with bit depth {bd}")
    if interlace != 0:
        raise ValueError("the PNG is interlaced, which is not supported; "
                         "save it again with interlacing turned off")
    raw = zlib.decompress(idat)
    ch = 4
    stride = w * ch

    def paeth(a, b, c):
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        return a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)

    out = bytearray()
    prev = bytearray(stride)
    i = 0
    for _ in range(h):
        f = raw[i]; i += 1
        line = bytearray(raw[i:i + stride]); i += stride
        for x in range(stride):
            a = line[x - ch] if x >= ch else 0
            b = prev[x]
            c = prev[x - ch] if x >= ch else 0
            if f == 1: line[x] = (line[x] + a) & 0xFF
            elif f == 2: line[x] = (line[x] + b) & 0xFF
            elif f == 3: line[x] = (line[x] + ((a + b) >> 1)) & 0xFF
            elif f == 4: line[x] = (line[x] + paeth(a, b, c)) & 0xFF
        out += line
        prev = line
    px = [tuple(out[(y * w + x) * ch:(y * w + x) * ch + ch]) for y in range(h) for x in range(w)]
    return w, h, px


def rgb_to_bgr555(r: int, g: int, b: int) -> int:
    return ((b >> 3) << 10) | ((g >> 3) << 5) | (r >> 3)


def bgr555_to_rgb(v: int) -> tuple[int, int, int]:
    return ((v & 31) * 8, ((v >> 5) & 31) * 8, ((v >> 10) & 31) * 8)


def derive_palette(px) -> list[int]:
    """Provisional 16-color palette from the opaque image colors (index 0 = transparent)."""
    seen: list[int] = []
    for (r, g, b, a) in px:
        if a == 0:
            continue
        c = rgb_to_bgr555(r, g, b)
        if c not in seen:
            seen.append(c)
    if len(seen) > 15:
        raise ValueError(f"image has {len(seen)} opaque colours; 4bpp allows 15 (+transparent)")
    return [0] + seen + [0] * (15 - len(seen))


def nearest_index(r: int, g: int, b: int, pal_rgb: list[tuple[int, int, int]]) -> int:
    """Return the index from 1 to 15 whose palette color is nearest to ``(r, g, b)``."""
    best, bi = 1 << 30, 1
    for idx in range(1, 16):  # never map opaque pixels to index 0 (transparent)
        pr, pg, pb = pal_rgb[idx]
        d = (pr - r) ** 2 + (pg - g) ** 2 + (pb - b) ** 2
        if d < best:
            best, bi = d, idx
    return bi


def to_indices(px, w, h, pal: list[int]) -> list[list[int]]:
    """Quantize RGBA pixels to a grid of indices into ``pal``, a list of 16 BGR555 colors.

    A pixel with alpha 0 becomes index 0, which is transparent. Every other pixel becomes the
    index of its nearest palette color, so it is never index 0."""
    pal_rgb = [bgr555_to_rgb(c) for c in pal]
    grid = [[0] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[y * w + x]
            grid[y][x] = 0 if a == 0 else nearest_index(r, g, b, pal_rgb)
    return grid


def tile_4bpp(grid, tx: int, ty: int) -> bytes:
    """One 8x8 4bpp tile at tile-column tx, tile-row ty. Low nibble = left pixel."""
    out = bytearray()
    for row in range(8):
        for col in range(0, 8, 2):
            lo = grid[ty * 8 + row][tx * 8 + col]
            hi = grid[ty * 8 + row][tx * 8 + col + 1]
            out.append((hi << 4) | lo)
    return bytes(out)


def fit_16x16(grid: list[list[int]]) -> list[list[int]]:
    """Crop an index grid to its opaque content, and center that content in a 16x16 grid.

    Index 0 is transparent and every other index is opaque. The source can be any size, such as a
    32x48 object frame exported from DSVEdit, as long as its opaque content fits in 16x16. Raises
    ValueError when the grid has no opaque pixels, or when its opaque content is larger than
    16x16."""
    opaque = [(x, y) for y in range(len(grid)) for x in range(len(grid[0])) if grid[y][x] != 0]
    if not opaque:
        raise ValueError("icon source is fully transparent")
    x0 = min(x for x, _ in opaque); x1 = max(x for x, _ in opaque)
    y0 = min(y for _, y in opaque); y1 = max(y for _, y in opaque)
    cw, ch = x1 - x0 + 1, y1 - y0 + 1
    if cw > 16 or ch > 16:
        raise ValueError(f"opaque content is {cw}x{ch} pixels, which does not fit in a 16x16 icon")
    ox, oy = (16 - cw) // 2, (16 - ch) // 2
    out = [[0] * 16 for _ in range(16)]
    for yy in range(ch):
        for xx in range(cw):
            out[oy + yy][ox + xx] = grid[y0 + yy][x0 + xx]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("png")
    ap.add_argument("--palette", default=None,
                    help="16 BGR555 hwords (comma/space separated) = OBJ bank 6; omit to derive")
    args = ap.parse_args()
    with open(args.png, "rb") as f:
        w, h, px = decode_png_rgba(f.read())

    if args.palette:
        toks = [t for t in args.palette.replace(",", " ").split() if t]
        pal = [int(t, 0) for t in toks]
        if len(pal) != 16:
            raise SystemExit(f"--palette needs 16 hwords, got {len(pal)}")
        provisional = False
    else:
        pal = derive_palette(px)
        provisional = True

    grid = to_indices(px, w, h, pal)
    if (w, h) != (16, 16):
        grid = fit_16x16(grid)   # crop transparent edges + center into 16x16
    t0, t1 = tile_4bpp(grid, 0, 0), tile_4bpp(grid, 1, 0)   # top-left, top-right
    t2, t3 = tile_4bpp(grid, 0, 1), tile_4bpp(grid, 1, 1)   # bottom-left, bottom-right
    top, bottom = t0 + t1, t2 + t3                          # two 0x40 halves
    blob = top + bottom
    assert len(blob) == 0x80

    print(f"# icon tiles for {args.png}  ({'PROVISIONAL image palette' if provisional else 'bank-6 palette'})")
    print(f"# 0x80 bytes: top half (0x40) DMAd to slot, bottom half (0x40) to slot+0x200")
    print('ICON_TILES = bytes.fromhex(')
    print(f'    "{top.hex()}"   # top  (tiles 0,1)')
    print(f'    "{bottom.hex()}" # bottom(tiles 2,3)')
    print(')')
    if provisional:
        print(f"# provisional palette (BGR555): {[hex(c) for c in pal]}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except ValueError as err:
        sys.exit(str(err))
