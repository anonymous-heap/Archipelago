"""
Pins for the ROM feature modules that declare their placements as bytemaker ``Entry`` objects
(``rom/soul_drop_rates.py``, ``rom/deathlink_hook.py``, ``rom/address_space.py``).

Numbers here are counted by hand from the ROM notes on purpose, so a drift in a declaration
fails here rather than in a mis-patched ROM. The byte *outputs* of these modules are pinned by
their own test files; this file covers the guarantees the declarations add: layout-derived
offsets, verified edits, and build-time fit checks.
"""
import unittest

from .._bytemaker_compat import Patch, UInt8, offset_of, sizeof
from ..rom import deathlink_hook as dh
from ..rom import soul_drop_rates as sdr
from ..rom import soul_shuffle as ss
from ..rom.address_space import ROM_SIZE, gba_space
from ..rom.enemy_table import EnemyDNA, field_offset, row_offset
from ..rom.entity import GBA_ROM_BASE


class AddressSpaceTest(unittest.TestCase):
    def test_cart_geometry(self):
        self.assertEqual(ROM_SIZE, 8 * 1024 * 1024)
        space = gba_space()
        self.assertEqual(space.offset(GBA_ROM_BASE + 0x670040), 0x670040)

    def test_bound_space_reads_the_image(self):
        image = bytearray(0x1000)
        image[0x123] = 0xAB
        self.assertEqual(gba_space(bytes(image)).read(GBA_ROM_BASE + 0x123, UInt8), 0xAB)


class EnemyTableLayoutTest(unittest.TestCase):
    def test_row_shape(self):
        self.assertEqual(sizeof(EnemyDNA), 0x24)
        for name, off in {"soul_rate": 0x12, "soul_type": 0x17, "soul_index": 0x18}.items():
            with self.subTest(field=name):
                self.assertEqual(offset_of(EnemyDNA, name), off)
        self.assertEqual((sdr.ENEMY_STRIDE, sdr.SOUL_RATE_OFF), (0x24, 0x12))
        self.assertEqual((ss.ENEMY_STRIDE, ss.SOUL_RATE_OFF, ss.SOUL_TYPE_OFF, ss.SOUL_INDEX_OFF),
                         (0x24, 0x12, 0x17, 0x18))

    def test_a_field_offset_is_its_row_plus_the_field(self):
        self.assertEqual(field_offset(54, "soul_type"), row_offset(54) + 0x17)

    def test_table_placement(self):
        self.assertEqual(sdr.ENEMY_TABLE_GBA, 0x080E9644)
        self.assertEqual(sdr.ENEMY_COUNT, 113)

    def test_rate_offset_is_the_documented_arithmetic(self):
        for enemy_id in (0, 43, 48, 54, sdr.ENEMY_COUNT - 1):
            with self.subTest(enemy=enemy_id):
                self.assertEqual(sdr._rate_offset(enemy_id),
                                 (0x080E9644 - GBA_ROM_BASE) + enemy_id * 0x24 + 0x12)

    def test_rate_offset_refuses_an_enemy_outside_the_table(self):
        with self.assertRaises(IndexError):
            sdr._rate_offset(sdr.ENEMY_COUNT)


class SoulDropOverridesTest(unittest.TestCase):
    def _rom_with(self, overrides_present: bool) -> bytes:
        rom = bytearray(sdr._rate_offset(sdr.ENEMY_COUNT - 1) + 1)
        if overrides_present:
            for enemy_id, (_name, old, _new) in sdr.SOUL_RATE_OVERRIDES.items():
                rom[sdr._rate_offset(enemy_id)] = old
        return bytes(rom)

    def test_writes_one_byte_per_override_at_the_rate_field(self):
        writes = sdr.build_writes(self._rom_with(True))
        self.assertEqual(writes, {sdr._rate_offset(e): bytes([new])
                                  for e, (_n, _old, new) in sdr.SOUL_RATE_OVERRIDES.items()})

    def test_mismatch_names_the_enemy_and_is_a_value_error(self):
        with self.assertRaises(ValueError) as caught:
            sdr.build_writes(self._rom_with(False))
        message = str(caught.exception)
        self.assertIn("soul-rate byte", message)
        self.assertIn("(ROM mismatch)", message)
        self.assertTrue(any(name in message for name, _o, _n in sdr.SOUL_RATE_OVERRIDES.values()))
        # the library's own verification failure is kept as the cause
        self.assertIsNotNone(caught.exception.__cause__)


class DeathlinkPlacementTest(unittest.TestCase):
    def test_entries_pin_the_documented_addresses(self):
        self.assertEqual(dh.HOOK_SITE.addr, 0x0801B9D0)
        self.assertEqual(dh.DEATHLINK_TRAMPOLINE.addr, 0x08670040)
        self.assertEqual((dh.HOOK_SITE_GBA, dh.TRAMPOLINE_GBA), (dh.HOOK_SITE.addr, dh.DEATHLINK_TRAMPOLINE.addr))

    def test_trampoline_fits_its_reservation_with_headroom(self):
        self.assertLessEqual(len(dh._TRAMPOLINE), 0xC0)

    def test_an_oversized_blob_refuses_to_build(self):
        space = gba_space()
        with self.assertRaises(ValueError):
            dh.DEATHLINK_TRAMPOLINE.bind(space).write(bytes(0xC1), patch=Patch(name="too big"))
        with self.assertRaises(ValueError):
            dh.HOOK_SITE.bind(space).write(bytes(9), patch=Patch(name="too big"))
