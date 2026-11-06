"""Structural tests for the received-item announcement (rom/received_item_box.py).

These do not run the ROM. They check the hook body's literal pool, its install in slot 2, and the
mailbox block layout. Whether the banner and sound actually appear for a received item still
needs an emulator playtest (see rom/received_item_box.s).
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


class TestBlob(unittest.TestCase):
    def test_the_literal_pool_is_exactly_the_eight_expected_words(self):
        # test_thumb_assembler checks the blob against its .s source, but that needs keystone
        # and skips without it. This pins the pool with no dependency, so a truncated blob or a
        # stray pool word still fails somewhere.
        pool = [
            rib.MAILBOX_GBA,
            0x02000420,           # the textbox request block (kind at +3, state at +0xC)
            0x03000200,           # the busy mask
            0x0800EF98 | 1,       # sub_0800EF98, the "Got <name>" textbox
            0x08045C34 | 1,       # sub_08045C34, the new-soul effect
            0x0800E708 | 1,       # sub_0800E708, the soul banner
            0x08049E64 | 1,       # sub_08049E64, the soul display queue
            0x080D7910 | 1,       # PlaySong
        ]
        expected = b"".join(struct.pack("<I", w) for w in pool)
        self.assertEqual(rib._HOOK_BODY[-len(expected):], expected)
        self.assertEqual(len(rib._HOOK_BODY) - len(expected), 112, "code size changed")

    def test_the_hook_waits_while_a_textbox_request_is_queued(self):
        # ldrb r1,[r0,#3]; cmp r1,#0; bne: the request kind at 0x02000423 is checked before the
        # busy bits (tst r0,r1). Queueing over a waiting request would replace its text.
        code = rib._HOOK_BODY[:-32]
        wait = code.find(bytes.fromhex("c1780029"))
        self.assertNotEqual(wait, -1, "the hook must check the queued request kind")
        self.assertEqual(code[wait + 4 + 1], 0xD1, "and skip the frame while it is nonzero")
        self.assertLess(wait, code.find(bytes.fromhex("0842")))

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
    """test_xanthus_framework covers the shared framework writes and how slots coexist."""

    def test_installs_the_hook_body_in_slot_2(self):
        writes = rib.build_writes(bytes(vanilla_rom()))
        self.assertEqual(writes[xf.slot_entry_offset(2)],
                         struct.pack("<I", xf.slot_body_gba(2) | 1))
        self.assertEqual(writes[xf.slot_body_gba(2) - GBA], rib._HOOK_BODY)


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


if __name__ == "__main__":
    unittest.main()
