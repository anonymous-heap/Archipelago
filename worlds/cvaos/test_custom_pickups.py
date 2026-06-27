"""Structural tests for the custom-pickup framework (rom/custom_pickups.py).

These do not run the ROM; they check the bytes the builder emits (blob shape, the spawn-path literal
repoint, the extended icon table, the descriptor rows, icon-tile placement, and no write overlaps).
A full in-game check (the button opening the A01 barrier + its SFX, and the recoloured icon) still
needs an emulator playtest -- see rom/custom_pickups.py.
"""
from __future__ import annotations

import struct
import unittest

import worlds.cvaos.rom.custom_pickups as cp

GBA = 0x08000000
# Synthetic vanilla consumable table: 32 entries x 0x10, entry n = [n, 0, icon=n+1, pal=6, 0...].
_FAKE_CONSUMABLES = b"".join(bytes([n, 0, n + 1, 6] + [0] * 12) for n in range(cp.CONSUMABLE_COUNT))


class TestBlob(unittest.TestCase):
    def test_blob_intact(self):
        self.assertEqual(len(cp.CUSTOMHOOK_BLOB), 152)
        # literal pool must bake the descriptor-table address the hook scans
        self.assertIn(cp.CUSTOM_DESC_TABLE_GBA.to_bytes(4, "little"), cp.CUSTOMHOOK_BLOB)

    def test_trampoline_targets_hook(self):
        w = cp.build_writes(_FAKE_CONSUMABLES)
        tramp = w[cp.HOOK_SITE_GBA - GBA]
        self.assertEqual(tramp[:4], bytes.fromhex("004b1847"))           # ldr r3,[pc,#0]; bx r3
        self.assertEqual(tramp[4:], struct.pack("<I", cp.CUSTOMHOOK_BASE_GBA | 1))  # .word base|thumb


class TestWrites(unittest.TestCase):
    def setUp(self):
        self.w = cp.build_writes(_FAKE_CONSUMABLES)

    def test_literal_repoint(self):
        self.assertEqual(self.w[cp.CONSUMABLE_ICON_LITERAL_FILE_OFFSET],
                         struct.pack("<I", cp.CUSTOM_ICON_TABLE_GBA))

    def test_extended_table_preserves_originals(self):
        ext = self.w[cp.CUSTOM_ICON_TABLE_GBA - GBA]
        self.assertEqual(ext[:0x200], _FAKE_CONSUMABLES)                  # existing items untouched
        for p in cp.CUSTOM_PICKUPS:
            base = p.item_offset * cp.CONSUMABLE_ENTRY_SIZE
            self.assertEqual(ext[base + cp.ITEM_ENTRY_ICON_OFF], p.icon_id)
            self.assertEqual(ext[base + cp.ITEM_ENTRY_PAL_OFF], cp.ITEMS_PALETTE_BANK)

    def test_descriptor_rows(self):
        dt = self.w[cp.CUSTOM_DESC_TABLE_GBA - GBA]
        for i, p in enumerate(cp.CUSTOM_PICKUPS):
            row = dt[i * 12:i * 12 + 12]
            self.assertEqual(struct.unpack("<HHHHHH", row)[:4],
                             (p.item_offset, p.flag_field, p.flag_number, p.sfx))
        self.assertIn(cp.DESC_TERMINATOR, dt)

    def test_button_sets_misc_flag_48(self):
        b = cp.FORBIDDEN_AREA_BUTTON
        self.assertEqual((b.flag_field, b.flag_number), (cp.FLAG_FIELD_MISC, 48))  # A01 barrier flag
        self.assertEqual(b.sfx, 0x133)

    def test_icon_tiles_split_into_two_halves(self):
        for p in cp.CUSTOM_PICKUPS:
            top, bottom = cp._icon_tile_file_offsets(p.icon_id)
            self.assertEqual(bottom - top, 0x200)
            self.assertEqual(self.w[top], p.icon_tiles[:cp.ICON_HALF])
            self.assertEqual(self.w[bottom], p.icon_tiles[cp.ICON_HALF:])

    def test_no_write_overlaps(self):
        spans = sorted((o, o + len(b)) for o, b in self.w.items())
        for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
            self.assertLessEqual(a1, b0, f"overlap {a0:#x}..{a1:#x} vs {b0:#x}..{b1:#x}")


class TestRegistry(unittest.TestCase):
    def test_encoding_is_consumable_pickup(self):
        self.assertEqual(cp.get_encoding(cp.FORBIDDEN_AREA_BUTTON), (4, 2, cp.FORBIDDEN_AREA_BUTTON.item_offset))

    def test_validation(self):
        with self.assertRaises(ValueError):  # item_offset must be >= 32 (new space, not a real item)
            cp.CustomPickup("x", 5, 0x1F, b"\0" * 0x80, cp.FLAG_FIELD_MISC, 48, 0)
        with self.assertRaises(ValueError):  # icon id must be in the free 0x1f..0x40 range
            cp.CustomPickup("x", 40, 0x05, b"\0" * 0x80, cp.FLAG_FIELD_MISC, 48, 0)
        with self.assertRaises(ValueError):  # icon tiles must be 0x80 bytes
            cp.CustomPickup("x", 40, 0x1F, b"\0" * 4, cp.FLAG_FIELD_MISC, 48, 0)


if __name__ == "__main__":
    unittest.main()
