"""
No AP free-space write may land in the Advance Collection ROM's M2 additions.

The collection's copy of the ROM carries M2's LZ77 replacement graphic at file
0x660000-0x6610BC (repointed via 0x1CC018) and its audio bridge at 0x700000-0x7000E3. Both
ranges are all-zero on a cart dump and live on the collection image, so a write that is
harmless on a cart dump corrupts the collection. The regions are declared in
``rom/address_space.py``; this test drives every feature's real write set against them.

    python -m pytest worlds/cvaos/test/test_m2_free_space.py -v
"""
from __future__ import annotations

import unittest

from ..rom import custom_pickups as cp
from ..rom import deathlink_hook as dh
from ..rom import inventory_menu as im
from ..rom import patch
from ..rom import skull_key_warp as skw
from ..rom import soul_guarantee_hook as sgh
from ..rom.address_space import M2_AUDIO_BRIDGE, M2_GRAPHIC, gba_space, m2_region_hit
from ..rom.entity import GBA_ROM_BASE


class M2RegionsTest(unittest.TestCase):
    def test_regions_match_the_measured_ranges(self):
        geometry = gba_space()
        self.assertEqual(tuple(M2_GRAPHIC.bind(geometry).request()), (0x660000, 0x10BD))
        self.assertEqual(tuple(M2_AUDIO_BRIDGE.bind(geometry).request()), (0x700000, 0xE4))

    def test_hit_detection_covers_edges(self):
        self.assertIs(m2_region_hit(0x660000, 1), M2_GRAPHIC)
        self.assertIs(m2_region_hit(0x6610BC, 1), M2_GRAPHIC)
        self.assertIsNone(m2_region_hit(0x6610BD, 1))
        self.assertIs(m2_region_hit(0x65FFF0, 0x20), M2_GRAPHIC)   # straddles the start
        self.assertIs(m2_region_hit(0x7000E3, 1), M2_AUDIO_BRIDGE)
        self.assertIsNone(m2_region_hit(0x7000E4, 1))


class M2FreeSpaceTest(unittest.TestCase):
    def _assert_clear(self, offset: int, length: int, what: str) -> None:
        region = m2_region_hit(offset, length)
        self.assertIsNone(region, f"{what} at 0x{offset:06X}+0x{length:X} lands in {region!r}")

    def test_metadata_block_is_clear(self):
        self._assert_clear(patch.ARCHIPELAGO_IDENTIFIER_START, len(patch.ARCHIPELAGO_IDENTIFIER), "identifier")
        self._assert_clear(patch.AUTH_NUMBER_START, 16, "auth number")

    def test_rom_free_write_sets_are_clear(self):
        site = sgh.HOOK_SITE.addr - GBA_ROM_BASE
        rom = bytearray(0x800000)
        rom[site:site + len(sgh.STOLEN)] = sgh.STOLEN
        write_sets = {
            "deathlink_hook": dh.build_writes(),
            "skull_key_warp": skw.build_writes(),
            "soul_guarantee_hook": sgh.build_writes(bytes(rom)),
        }
        for name, writes in write_sets.items():
            for offset, data in writes.items():
                with self.subTest(feature=name, offset=hex(offset)):
                    self._assert_clear(offset, len(data), name)

    def test_rom_reading_writers_placements_are_clear(self):
        # custom_pickups and inventory_menu read the base ROM (icon sprites, text tables) to build their
        # writes, so their placements are checked from the constants and the ROM-free parts of the payload.
        self._assert_clear(cp.CUSTOMHOOK_BASE_GBA - GBA_ROM_BASE, len(cp.CUSTOMHOOK_BLOB), "custom-pickup dispatcher")
        self._assert_clear(cp.CUSTOM_DESC_TABLE_GBA - GBA_ROM_BASE, len(cp._desc_table(cp._registry())),
                           "custom-pickup descriptor table")
        # The extended icon table holds at least MAX_SHADOW_SLOT + 1 entries of CONSUMABLE_ENTRY_SIZE bytes.
        self._assert_clear(cp.CUSTOM_ICON_TABLE_GBA - GBA_ROM_BASE, 0x2C0, "extended icon table")
        self._assert_clear(im.MENU_BLOB_GBA - GBA_ROM_BASE, len(im.MENU_BLOB), "menu blob")
        for name in ("EXT_NAME_TABLE_GBA", "EXT_DESC_TABLE_GBA", "EXT_STRINGPTR_GBA", "NEW_STRINGS_GBA"):
            with self.subTest(table=name):
                self._assert_clear(getattr(im, name) - GBA_ROM_BASE, 1, name)
