"""Unit tests for the custom-pickup icon pipeline (rom/icon.py).

These use synthetic data rather than a ROM. They exercise the GBA LZ77 decompressor, the 1D
sprite-page addressing, the source-index -> bank-6-index remap, and the 0x80-byte icon packing.
ImageFile icons are resolved from PNGs built in memory, including PNGs that must raise ValueError
during generation. The real metal-gate-button extraction (RomSprite against the player's ROM) is
exercised end-to-end by build_writes_from_rom and the emulator playtest, not here.
"""
from __future__ import annotations

import struct
import unittest
import zlib
from unittest import mock

import worlds.cvaos.rom.icon as ic
from worlds.cvaos.rom.custom_pickups import BANK6_PALETTE
from worlds.cvaos.rom.tools.png_to_icon import bgr555_to_rgb, fit_16x16


def _lz77_literals(data: bytes) -> bytes:
    """A minimal GBA LZ77 (type 0x10) stream that stores ``data`` as all-literal blocks."""
    out = bytearray([0x10, len(data) & 0xFF, (len(data) >> 8) & 0xFF, (len(data) >> 16) & 0xFF])
    for i in range(0, len(data), 8):
        out.append(0x00)            # flag byte: 8 literals follow
        out += data[i:i + 8]
    return bytes(out)


def _png(w: int, h: int, pixels: dict, *, color_type: int = 6, interlace: int = 0) -> bytes:
    """Encode a w x h PNG in memory, with 8-bit RGBA pixel data.

    ``pixels`` maps ``(x, y)`` to an RGBA tuple, and every other pixel is transparent. The
    ``color_type`` and ``interlace`` arguments change only the IHDR header, while the pixel data
    stays non-interlaced RGBA. That is enough for tests that expect the header to be rejected."""
    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))

    rows = (b"\x00" + b"".join(bytes(pixels.get((x, y), (0, 0, 0, 0))) for x in range(w))
            for y in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, interlace)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


def _icon_file(png: bytes):
    """Patch rom/icon.py so that the ImageFile it reads holds ``png``, without touching rom/icons/."""
    return mock.patch.object(ic, "open", mock.mock_open(read_data=png), create=True)


class TestIcon(unittest.TestCase):
    def test_lz77_roundtrip(self):
        data = bytes(range(256)) * 3            # 768 bytes, forces multiple flag blocks
        rom = b"\xAA\xBB" + _lz77_literals(data)  # stream not at offset 0
        self.assertEqual(ic.lz77_decompress(rom, 2), data)

    def test_lz77_backref(self):
        # hand-built stream: literal 'A', then a backref (len=(1)+3=4, disp=1) copying it 4x -> "AAAAA"
        stream = bytes([0x10, 5, 0, 0, 0b01000000, ord("A"), 0x10, 0x00])
        out = ic.lz77_decompress(stream, 0)
        self.assertEqual(out, b"AAAAA")

    def test_rom_sprite_extract_remap_and_pack(self):
        # A sprite page has 16 tiles per row, so the 16x16 part at (0, 0) is page tiles 0 and 1
        # on top and 16 and 17 below. In each byte, the low nibble is the left pixel of a pair.
        # Each of these tiles repeats one byte, and every other tile is empty.
        tile_bytes = {0: 0x31, 1: 0x22, 16: 0x13, 17: 0x22}
        tiles = [bytes([tile_bytes.get(t, 0)] * 32) for t in range(18)]
        rom = _lz77_literals(b"".join(tiles))
        spr = ic.RomSprite(gfx_addr=ic.GBA_ROM_BASE, part=(0, 0, 16, 16), remap={1: 7, 2: 5},
                           crop=False)
        grid = ic.rom_sprite_indices(rom, spr)
        # Index 3 has no remap entry, so it becomes 0 (transparent).
        top_row = [7, 0] * 4 + [5] * 8
        bottom_row = [0, 7] * 4 + [5] * 8
        self.assertEqual(grid, [top_row] * 8 + [bottom_row] * 8)
        # The icon packs its tiles as top-left, top-right, bottom-left, bottom-right.
        self.assertEqual(ic.pack_icon(grid),
                         bytes([0x07] * 32 + [0x55] * 32 + [0x70] * 32 + [0x55] * 32))

    def test_uncropped_rom_sprite_region_must_be_16x16(self):
        with self.assertRaisesRegex(ValueError, "16x11"):
            ic.RomSprite(gfx_addr=ic.GBA_ROM_BASE, part=(32, 5, 16, 11), remap={}, crop=False)
        # With cropping, the same region is accepted, because only its opaque content must fit.
        ic.RomSprite(gfx_addr=ic.GBA_ROM_BASE, part=(32, 5, 16, 11), remap={}, crop=True)

    def test_crop_centers_opaque(self):
        grid = [[0] * 16 for _ in range(16)]
        grid[1][1] = 5                                              # single opaque pixel near a corner
        out = fit_16x16(grid)
        self.assertEqual(sum(v != 0 for row in out for v in row), 1)
        self.assertEqual(out[7][7], 5)                              # 1x1 content centers to (7,7)

    def test_image_file_quantizes_and_centers(self):
        # Two pixels of exact bank-6 colors in a 32x32 image: gold (index 7) then white (15).
        gold, white = (bgr555_to_rgb(BANK6_PALETTE[i]) + (255,) for i in (7, 15))
        with _icon_file(_png(32, 32, {(20, 3): gold, (21, 3): white})):
            grid = ic.image_file_indices(ic.ImageFile("test.png"))
        expected = [[0] * 16 for _ in range(16)]
        expected[7][7], expected[7][8] = 7, 15                      # 2x1 content centers at (7,7)
        self.assertEqual(grid, expected)

    def test_image_file_rejects_interlaced_png(self):
        png = _png(16, 16, {(0, 0): (255, 255, 255, 255)}, interlace=1)
        with _icon_file(png), self.assertRaisesRegex(ValueError, r"test\.png: .*interlaced"):
            ic.build_icon_tiles(b"", ic.ImageFile("test.png"))

    def test_image_file_rejects_non_rgba_png(self):
        png = _png(16, 16, {}, color_type=2)                       # color type 2 is RGB
        with _icon_file(png), self.assertRaisesRegex(ValueError, "color type 2"):
            ic.build_icon_tiles(b"", ic.ImageFile("test.png"))

    def test_image_file_rejects_content_larger_than_16x16(self):
        png = _png(20, 16, {(x, 0): (255, 255, 255, 255) for x in range(17)})
        with _icon_file(png), self.assertRaisesRegex(ValueError, "17x1"):
            ic.build_icon_tiles(b"", ic.ImageFile("test.png"))


if __name__ == "__main__":
    unittest.main()
