"""Unit tests for the custom-pickup icon pipeline (rom/icon.py).

These use synthetic GFX (no ROM): they exercise the GBA LZ77 decompressor, the 1D sprite-page
addressing, the source-index -> bank-6-index remap, and the 0x80-byte icon packing. The real
metal-gate-button extraction (RomSprite against the player's ROM) is exercised end-to-end by
build_writes_from_rom and the emulator playtest, not here.
"""
from __future__ import annotations

import unittest

import worlds.cvaos.rom.icon as ic


def _lz77_literals(data: bytes) -> bytes:
    """A minimal GBA LZ77 (type 0x10) stream that stores ``data`` as all-literal blocks."""
    out = bytearray([0x10, len(data) & 0xFF, (len(data) >> 8) & 0xFF, (len(data) >> 16) & 0xFF])
    for i in range(0, len(data), 8):
        out.append(0x00)            # flag byte: 8 literals follow
        out += data[i:i + 8]
    return bytes(out)


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

    def test_crop_centres_opaque(self):
        grid = [[0] * 16 for _ in range(16)]
        grid[1][1] = 5                                              # single opaque pixel near a corner
        out = ic._fit_16x16(grid)
        self.assertEqual(sum(v != 0 for row in out for v in row), 1)
        self.assertEqual(out[7][7], 5)                              # 1x1 content centres to (7,7)


if __name__ == "__main__":
    unittest.main()
