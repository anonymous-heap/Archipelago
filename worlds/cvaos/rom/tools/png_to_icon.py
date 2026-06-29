#!/usr/bin/env python3
"""
Convert a 16x16 RGBA PNG into an AoS "common item icon": four 4bpp GBA tiles laid out as the
two 0x40-byte halves the engine DMAs (top row = tiles 0,1; bottom row = tiles 2,3), against a
given 16-colour OBJ palette.

AoS pickup icons are loaded on demand by DMAing the 0x40 bytes (top half) from
sheet_base+off and another 0x40 (bottom half) from sheet_base+off+0x200. So a 16x16 icon is
0x80 bytes split into two 0x40 blocks; this tool emits that 0x80-byte blob.

Palette: floor pickups all render through OBJ palette bank 6 -- the shared items palette, loaded for
every pickup in every room (so it never gets clobbered). Bank 6 is items-palette sub-palette 2: its
16 colours are static in the ROM at 0x082099FC + 4 + 2*0x20 = 0x08209A40 (a rainbow incl. gold
#F8D848, tan #F8B070, greys #A0A0A8/#E8D8D0). Pass them via --palette (BGR555 hwords; they are also
kept as ``BANK6_PALETTE`` in rom/custom_pickups.py). Index 0 is transparent. If --palette is omitted,
a palette is derived from the image (provisional structure only; in-game colours wrong until re-run
against bank 6). Nearest-colour can pull mid-tones to off-hues (e.g. dark brass -> brown/red); for a
deliberate look, pre-map the source colours (see how custom_pickups._BUTTON_TILES was generated).

Usage:
  python png_to_icon.py button_pickup.png                       # provisional (image's own colours)
  python png_to_icon.py button_pickup.png --palette "0,0x7fff,..."   # final (bank 6 colours)
"""
from __future__ import annotations

import argparse
import struct
import sys
import zlib


def decode_png_rgba(path: str) -> tuple[int, int, list[tuple[int, int, int, int]]]:
    data = open(path, "rb").read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos = 8
    w = h = bd = ct = None
    idat = b""
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w, h, bd, ct = struct.unpack(">IIBB", chunk[:10])
        elif typ == b"IDAT":
            idat += chunk
        elif typ == b"IEND":
            break
        pos += 12 + ln
    assert ct == 6 and bd == 8, f"expected 8-bit RGBA, got colortype={ct} depth={bd}"
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
    """Provisional 16-colour palette from the opaque image colours (index 0 = transparent)."""
    seen: list[int] = []
    for (r, g, b, a) in px:
        if a == 0:
            continue
        c = rgb_to_bgr555(r, g, b)
        if c not in seen:
            seen.append(c)
    if len(seen) > 15:
        raise SystemExit(f"image has {len(seen)} opaque colours; 4bpp allows 15 (+transparent)")
    return [0] + seen + [0] * (15 - len(seen))


def nearest_index(r: int, g: int, b: int, pal_rgb: list[tuple[int, int, int]]) -> int:
    best, bi = 1 << 30, 1
    for idx in range(1, 16):  # never map opaque pixels to index 0 (transparent)
        pr, pg, pb = pal_rgb[idx]
        d = (pr - r) ** 2 + (pg - g) ** 2 + (pb - b) ** 2
        if d < best:
            best, bi = d, idx
    return bi


def to_indices(px, w, h, pal: list[int]) -> list[list[int]]:
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


def fit_16x16(w, h, px):
    """Crop the opaque bounding box and centre it in a 16x16 transparent canvas. Lets a tightly- or
    loosely-cropped source (e.g. a 32x48 DSVEdit object frame) become a 16x16 icon."""
    opaque = [(x, y) for y in range(h) for x in range(w) if px[y * w + x][3] != 0]
    if not opaque:
        raise SystemExit("image is fully transparent")
    x0 = min(x for x, _ in opaque); x1 = max(x for x, _ in opaque)
    y0 = min(y for _, y in opaque); y1 = max(y for _, y in opaque)
    cw, ch = x1 - x0 + 1, y1 - y0 + 1
    if cw > 16 or ch > 16:
        raise SystemExit(f"opaque content is {cw}x{ch}; must be <= 16x16 to fit an icon")
    ox, oy = (16 - cw) // 2, (16 - ch) // 2          # centre
    out = [(0, 0, 0, 0)] * (16 * 16)
    for y in range(ch):
        for x in range(cw):
            out[(oy + y) * 16 + (ox + x)] = px[(y0 + y) * w + (x0 + x)]
    return 16, 16, out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("png")
    ap.add_argument("--palette", default=None,
                    help="16 BGR555 hwords (comma/space separated) = OBJ bank 6; omit to derive")
    args = ap.parse_args()
    w, h, px = decode_png_rgba(args.png)
    if (w, h) != (16, 16):
        w, h, px = fit_16x16(w, h, px)   # crop transparent edges + centre into 16x16

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
    main()
