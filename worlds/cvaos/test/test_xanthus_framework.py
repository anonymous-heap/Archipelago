"""Structural tests for the shared update-hook framework (rom/xanthus_framework.py).

The framework owns the one per-frame pointer every hook has to share, so these cover the part
that decides whether two independent features can coexist: the framework writes are idempotent
(a second feature finds them installed and emits the same bytes), while slot claims are strict.
"""
from __future__ import annotations

import struct
import unittest

import worlds.cvaos.rom.xanthus_framework as xf

GBA = 0x08000000
STUB = bytes.fromhex("7047")   # bx lr


def vanilla_rom() -> bytearray:
    rom = bytearray(0x800000)
    rom[xf.HOOK_SITE_OFFSET:xf.HOOK_SITE_OFFSET + 3] = xf.HOOK_SITE_VANILLA
    return rom


def apply(work: bytearray, writes) -> None:
    for offset, data in writes.items():
        work[offset:offset + len(data)] = data


class TestSlotMath(unittest.TestCase):
    def test_slot_addresses(self):
        self.assertEqual(xf.slot_body_gba(1), 0x087D0100)
        self.assertEqual(xf.slot_body_gba(2), 0x087D0300)
        self.assertEqual(xf.slot_body_gba(12), 0x087D0100 + 0x200 * 11)
        self.assertEqual(xf.slot_entry_offset(1), 0x7D0040)
        self.assertEqual(xf.slot_entry_offset(2), 0x7D0044)

    def test_slots_do_not_overlap(self):
        spans = [range(xf.slot_body_gba(s), xf.slot_body_gba(s) + xf.SLOT_BODY_STRIDE)
                 for s in range(1, xf.SLOT_COUNT + 1)]
        for i, a in enumerate(spans):
            for b in spans[i + 1:]:
                self.assertFalse(set(a) & set(b))

    def test_rejects_a_slot_outside_the_list(self):
        for bad in (0, 13, -1):
            with self.subTest(slot=bad), self.assertRaises(ValueError):
                xf.slot_body_gba(bad)


class TestWrites(unittest.TestCase):
    def test_installs_framework_and_slot(self):
        w = xf.writes(bytes(vanilla_rom()), 2, STUB)
        self.assertEqual(w[xf.HOOK_SITE_OFFSET], xf.HOOK_SITE_PATCHED)
        self.assertEqual(w[xf.DISPATCHER_OFFSET], xf.DISPATCHER)
        self.assertEqual(w[xf.slot_entry_offset(2)], struct.pack("<I", xf.slot_body_gba(2) | 1))
        self.assertEqual(w[xf.slot_body_gba(2) - GBA], STUB)

    def test_two_features_coexist_in_either_order(self):
        for first, second in ((1, 2), (2, 1)):
            with self.subTest(order=(first, second)):
                rom = vanilla_rom()
                apply(rom, xf.writes(bytes(rom), first, STUB))
                apply(rom, xf.writes(bytes(rom), second, STUB))
                for slot in (first, second):
                    at = xf.slot_entry_offset(slot)
                    self.assertEqual(bytes(rom[at:at + 4]),
                                     struct.pack("<I", xf.slot_body_gba(slot) | 1))

    def test_reinstalling_the_framework_is_idempotent(self):
        rom = vanilla_rom()
        apply(rom, xf.writes(bytes(rom), 1, STUB))
        w = xf.writes(bytes(rom), 2, STUB)          # framework already installed
        self.assertEqual(w[xf.HOOK_SITE_OFFSET], xf.HOOK_SITE_PATCHED)
        self.assertEqual(w[xf.DISPATCHER_OFFSET], xf.DISPATCHER)

    def test_refuses_a_double_claimed_slot(self):
        rom = vanilla_rom()
        apply(rom, xf.writes(bytes(rom), 2, STUB))
        with self.assertRaisesRegex(ValueError, "slot 2 is already claimed"):
            xf.writes(bytes(rom), 2, STUB)

    def test_refuses_a_foreign_hook_site(self):
        rom = vanilla_rom()
        rom[xf.HOOK_SITE_OFFSET:xf.HOOK_SITE_OFFSET + 3] = b"\x11\x22\x33"
        with self.assertRaisesRegex(ValueError, "hook-site"):
            xf.writes(bytes(rom), 1, STUB)

    def test_refuses_a_foreign_dispatcher(self):
        rom = vanilla_rom()
        rom[xf.DISPATCHER_OFFSET:xf.DISPATCHER_OFFSET + 4] = b"\xde\xad\xbe\xef"
        with self.assertRaisesRegex(ValueError, "other than the dispatcher"):
            xf.writes(bytes(rom), 1, STUB)

    def test_refuses_an_occupied_body_region(self):
        rom = vanilla_rom()
        rom[xf.slot_body_gba(3) - GBA] = 0xFF
        with self.assertRaisesRegex(ValueError, "not empty"):
            xf.writes(bytes(rom), 3, STUB)

    def test_refuses_an_oversized_body(self):
        with self.assertRaisesRegex(ValueError, "over the"):
            xf.writes(bytes(vanilla_rom()), 1, bytes(xf.SLOT_BODY_STRIDE + 1))


if __name__ == "__main__":
    unittest.main()
