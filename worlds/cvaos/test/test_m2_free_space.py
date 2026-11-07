"""
No AP free-space write may land in the Advance Collection ROM's M2 additions.

The collection's copy of the ROM carries M2's LZ77 replacement graphic at file
0x660000-0x6610BC (repointed via 0x1CC018) and its audio bridge at 0x700000-0x7000E3. Both
ranges are all-zero on a cart dump and live on the collection image, so a write that is
harmless on a cart dump corrupts the collection. The regions are declared in
``rom/address_space.py``; this test drives every feature's real write set against them.

AP's scratch areas in high EWRAM must likewise avoid the addresses that M2's code writes on the
collection (``M2_EWRAM``).

    python -m pytest worlds/cvaos/test/test_m2_free_space.py -v
"""
from __future__ import annotations

import unittest

from ..rom import custom_pickups as cp
from ..rom import deathlink_hook as dh
from ..rom import inventory_menu as im
from ..rom import identifiers
from ..rom import skull_key_warp as skw
from ..rom import soul_guarantee_hook as sgh
from ..rom import received_item_box as rib
from ..rom.address_space import M2_AUDIO_BRIDGE, M2_EWRAM, M2_GRAPHIC, gba_space, m2_region_hit
from ..rom.entity import GBA_ROM_BASE


def free_rom_spans() -> list[tuple[str, int, int]]:
    """Every AP placement in free ROM, as ``(name, file offset, length)``.

    A placement with a declared reservation is listed at its reserved size, because its blob may
    grow to fill it. custom_pickups and inventory_menu read the base ROM (icon sprites, text
    tables) to build their writes, so their spans come from the constants and the ROM-free parts
    of each payload.
    """
    return [
        ("identifier", identifiers.ARCHIPELAGO_IDENTIFIER_START,
         len(identifiers.ARCHIPELAGO_IDENTIFIER)),
        ("auth number", identifiers.AUTH_NUMBER_START, 16),
        ("deathlink trampoline", dh.DEATHLINK_TRAMPOLINE.addr - GBA_ROM_BASE,
         dh.DEATHLINK_TRAMPOLINE.size),
        ("Skull Key warp hook", skw.WARPHOOK_BASE_FILE_OFFSET, len(skw.WARPHOOK_BLOB)),
        ("Skull Key description", skw.DESC_STRING_FILE_OFFSET,
         len(skw.build_writes()[skw.DESC_STRING_FILE_OFFSET])),
        ("custom-pickup dispatcher", cp.CUSTOMHOOK_BASE_GBA - GBA_ROM_BASE, len(cp.CUSTOMHOOK_BLOB)),
        ("custom-pickup descriptor table", cp.CUSTOM_DESC_TABLE_GBA - GBA_ROM_BASE,
         len(cp._desc_table(cp._registry()))),
        ("menu blob", im.MENU_BLOB_GBA - GBA_ROM_BASE, len(im.MENU_BLOB)),
        ("soul guarantee trampoline", sgh.GUARANTEE_TRAMPOLINE.addr - GBA_ROM_BASE,
         sgh.GUARANTEE_TRAMPOLINE.size),
        ("Ancient Book descriptions", sgh.BOOK_DESCRIPTIONS.addr - GBA_ROM_BASE,
         sgh.BOOK_DESCRIPTIONS.size),
        # The extended icon table holds at least MAX_SHADOW_SLOT + 1 entries of
        # CONSUMABLE_ENTRY_SIZE bytes.
        ("extended icon table", cp.CUSTOM_ICON_TABLE_GBA - GBA_ROM_BASE, 0x2C0),
        # inventory_menu sizes its relocated tables from the base ROM, so only their first bytes
        # are listed.
        ("EXT_NAME_TABLE_GBA", im.EXT_NAME_TABLE_GBA - GBA_ROM_BASE, 1),
        ("EXT_DESC_TABLE_GBA", im.EXT_DESC_TABLE_GBA - GBA_ROM_BASE, 1),
        ("EXT_STRINGPTR_GBA", im.EXT_STRINGPTR_GBA - GBA_ROM_BASE, 1),
        ("NEW_STRINGS_GBA", im.NEW_STRINGS_GBA - GBA_ROM_BASE, 1),
    ]


class M2RegionsTest(unittest.TestCase):
    def test_regions_match_the_measured_ranges(self):
        geometry = gba_space()
        self.assertEqual(tuple(M2_GRAPHIC.bind(geometry).request()), (0x660000, 0x10BD))
        self.assertEqual(tuple(M2_AUDIO_BRIDGE.bind(geometry).request()), (0x700000, 0xE4))

    def test_ap_scratch_ewram_avoids_m2s(self):
        # The game never uses the high-EWRAM tail, but on the collection M2's code writes into
        # part of it. Every AP scratch area there has to stay clear of those writes.
        scratch = {
            "item-use shadow inventory": range(cp.SHADOW_INVENTORY_GBA,
                                               cp.SHADOW_INVENTORY_GBA + cp.MAX_SHADOW_SLOT + 1),
            "announcement mailbox": range(rib.MAILBOX_GBA, rib.MAILBOX_GBA + rib.MAILBOX_SIZE),
            "classicvania movement scratch": range(0x0203E000, 0x0203E004),
        }
        for name, area in scratch.items():
            for m2 in M2_EWRAM:
                with self.subTest(scratch=name, m2=hex(m2.start)):
                    self.assertFalse(area.start < m2.stop and m2.start < area.stop)

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

    def test_free_rom_spans_are_clear(self):
        for name, offset, length in free_rom_spans():
            with self.subTest(span=name):
                self._assert_clear(offset, length, name)

    def test_free_rom_spans_do_not_overlap(self):
        spans = sorted(free_rom_spans(), key=lambda span: span[1])
        for (name, offset, length), (next_name, next_offset, _length) in zip(spans, spans[1:]):
            with self.subTest(span=name):
                self.assertLessEqual(offset + length, next_offset,
                                     f"{name} at 0x{offset:06X}+0x{length:X} runs into {next_name}")

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
