"""Structural tests for the received-item announcement (rom/received_item_box.py).

These do not run the ROM; they check the bytes the builder emits (blob shape and literal pool,
the framework writes, the slot-2 registration, and the mailbox block layout). Whether the banner
and sound actually appear for a received item still needs an emulator playtest -- see
rom/received_item_box.s.
"""
from __future__ import annotations

import struct
import unittest

import worlds.cvaos.rom.received_item_box as rib
import worlds.cvaos.rom.xanthus_framework as xf

GBA = 0x08000000
ROM_SIZE = 0x800000


def vanilla_rom() -> bytearray:
    """A ROM-shaped buffer that is vanilla where this module looks: the per-frame pointer at
    0x043104 and zeros in the free-space regions."""
    rom = bytearray(ROM_SIZE)
    rom[0x043104:0x043107] = bytes.fromhex("6d3004")
    return rom


def apply(work: bytearray, writes) -> None:
    for offset, data in writes.items():
        work[offset:offset + len(data)] = data


class TestBlob(unittest.TestCase):
    def test_blob_shape(self):
        self.assertEqual(len(rib._HOOK_BODY), 136)
        self.assertLessEqual(len(rib._HOOK_BODY), 0x200, "body overruns its slot")

    def test_the_literal_pool_is_exactly_the_eight_expected_words(self):
        # Asserting the whole tail, not just "each address appears somewhere", is what catches a
        # truncated blob or a stray pool word from a bad hand-assembly.
        pool = [
            rib.MAILBOX_GBA,
            0x0200042C,           # the textbox state word
            0x03000200,           # the busy mask
            0x0800EF98 | 1,       # sub_0800EF98, the "Got <name>" textbox
            0x08045C34 | 1,       # sub_08045C34, the new-soul effect
            0x0800E708 | 1,       # sub_0800E708, the soul banner
            0x08049E64 | 1,       # sub_08049E64, the soul display queue
            0x080D7910 | 1,       # PlaySong
        ]
        expected = b"".join(struct.pack("<I", w) for w in pool)
        self.assertEqual(rib._HOOK_BODY[-len(expected):], expected)
        self.assertEqual(len(rib._HOOK_BODY) - len(expected), 104, "code size changed")

    def test_literal_pool_bakes_every_target(self):
        for name, addr in (
            ("mailbox", rib.MAILBOX_GBA),
            ("textbox state", 0x0200042C),
            ("busy mask", 0x03000200),
            ("sub_0800EF98|1", 0x0800EF98 | 1),
            ("sub_0800E708|1", 0x0800E708 | 1),
            ("sub_08049E64|1", 0x08049E64 | 1),
            ("sub_08045C34|1", 0x08045C34 | 1),
            ("PlaySong|1", 0x080D7910 | 1),
        ):
            with self.subTest(literal=name):
                self.assertIn(struct.pack("<I", addr), rib._HOOK_BODY)

    def test_mailbox_is_in_the_free_ewram_tail(self):
        # Above the gEwramData struct (ends 0x02025554), below the end of EWRAM, and clear of
        # this world's other claims in that tail.
        self.assertGreaterEqual(rib.MAILBOX_GBA, 0x02025554)
        self.assertLessEqual(rib.MAILBOX_GBA + rib.MAILBOX_SIZE, 0x02040000)
        shadow = range(0x02030000, 0x0203002C)
        mine = range(rib.MAILBOX_GBA, rib.MAILBOX_GBA + rib.MAILBOX_SIZE)
        self.assertFalse(set(shadow) & set(mine), "mailbox overlaps the Item-Use shadow array")
        self.assertNotIn(0x0203E000, mine, "mailbox overlaps classicvania_movement's scratch")


class TestWrites(unittest.TestCase):
    def setUp(self):
        self.rom = vanilla_rom()
        self.w = rib.build_writes(bytes(self.rom))

    def test_repoints_the_per_frame_pointer_at_the_dispatcher(self):
        self.assertEqual(self.w[0x043104], bytes.fromhex("01007d"))   # -> 0x087D0001

    def test_registers_in_slot_2(self):
        entry = self.w[0x7D0040 + 4 * (rib.HOOK_SLOT - 1)]
        self.assertEqual(entry, struct.pack("<I", rib.HOOK_BODY_GBA | 1))

    def test_body_lands_at_the_slot_address(self):
        self.assertEqual(self.w[rib.HOOK_BODY_GBA - GBA], rib._HOOK_BODY)


class TestFrameworkRegistration(unittest.TestCase):
    """This feature owns slot 2 of the shared framework and must not care who owns the others."""

    def test_registers_in_slot_2_of_the_shared_framework(self):
        self.assertEqual(rib.HOOK_SLOT, 2)
        self.assertEqual(rib.HOOK_BODY_GBA, xf.slot_body_gba(2))
        w = rib.build_writes(bytes(vanilla_rom()))
        self.assertEqual(w[xf.slot_entry_offset(2)],
                         struct.pack("<I", xf.slot_body_gba(2) | 1))

    def test_works_with_another_slot_already_installed(self):
        # patch.py hands each module the ROM as patched so far, so the framework may already be
        # in place with other slots claimed. Simulate a foreign slot-1 owner.
        rom = vanilla_rom()
        stub = bytes.fromhex("7047")                  # a stub body: bx lr
        apply(rom, xf.writes(bytes(rom), 1, stub))
        apply(rom, rib.build_writes(bytes(rom)))          # must not raise
        self.assertEqual(bytes(rom[xf.slot_entry_offset(1):xf.slot_entry_offset(1) + 4]),
                         struct.pack("<I", xf.slot_body_gba(1) | 1))
        self.assertEqual(bytes(rom[xf.slot_entry_offset(2):xf.slot_entry_offset(2) + 4]),
                         struct.pack("<I", xf.slot_body_gba(2) | 1))

    def test_installs_on_its_own(self):
        rom = vanilla_rom()
        apply(rom, rib.build_writes(bytes(rom)))
        self.assertEqual(bytes(rom[xf.DISPATCHER_OFFSET:xf.DISPATCHER_OFFSET + 52]),
                         xf.DISPATCHER)
        self.assertEqual(bytes(rom[0x043104:0x043107]), xf.HOOK_SITE_PATCHED)


class TestMailbox(unittest.TestCase):
    def test_item_block(self):
        block = rib.mailbox_write(0x0060)
        self.assertEqual(len(block), rib.MAILBOX_SIZE)
        self.assertEqual(block[rib.MB_ARG0:rib.MB_ARG0 + 2], struct.pack("<H", 0x0060))
        self.assertEqual(block[rib.MB_SFX:rib.MB_SFX + 2], struct.pack("<H", rib.SFX_ITEM))
        self.assertEqual(block[rib.MB_KIND], rib.KIND_ITEM)

    def test_soul_block_swaps_nothing_the_caller_gave(self):
        block = rib.soul_mailbox_write(soul_index=54, soul_type=2, is_new=False)
        self.assertEqual(block[rib.MB_ARG0:rib.MB_ARG0 + 2], struct.pack("<H", 54))
        self.assertEqual(block[rib.MB_KIND], rib.KIND_SOUL)
        self.assertEqual(block[rib.MB_ARG1], 2)      # soulType
        self.assertEqual(block[rib.MB_ARG2], 0)      # isNew
        self.assertEqual(block[rib.MB_SFX:rib.MB_SFX + 2], struct.pack("<H", rib.SFX_SOUL))

    def test_pending_is_the_final_byte(self):
        # The client writes the block in one transfer; pending last means the ROM can never see
        # a half-filled request.
        self.assertEqual(rib.MB_PENDING, rib.MAILBOX_SIZE - 1)
        for block in (rib.mailbox_write(1), rib.soul_mailbox_write(0, 0)):
            self.assertEqual(block[-1], 1)
            self.assertEqual(block[rib.MB_PENDING], 1)

    def test_out_of_range_arguments_are_refused(self):
        with self.assertRaises(ValueError):
            rib.mailbox_write(0x10000)
        with self.assertRaises(ValueError):
            rib.mailbox_write(1, sfx=-1)
        with self.assertRaises(ValueError):
            rib.soul_mailbox_write(soul_index=0, soul_type=0x100)


class TestNameTextId(unittest.TestCase):
    def test_reads_the_roms_own_table(self):
        rom = vanilla_rom()
        at = rib.ITEM_NAME_TEXT_IDS_GBA - GBA
        for gid in range(32):                        # consumables: text-id == 91 + slot
            rom[at + gid * 2:at + gid * 2 + 2] = struct.pack("<H", 91 + gid)
        self.assertEqual(rib.name_text_id(bytes(rom), 0), 91)
        self.assertEqual(rib.name_text_id(bytes(rom), 5), 96)

    def test_rejects_an_out_of_range_global_id(self):
        with self.assertRaises(ValueError):
            rib.name_text_id(bytes(vanilla_rom()), 257)


if __name__ == "__main__":
    unittest.main()
