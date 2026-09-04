"""Tests for announcing a received item in game (announce.py).

The announcement is cosmetic, so these check both that it posts the right request and that it
gets out of the way when it cannot: a mailbox the ROM has not drained yet, an item with no
banner, and a backend that fails outright.
"""
from __future__ import annotations

import asyncio
import unittest

from worlds.cvaos import announce
from worlds.cvaos.data.item_info import by_item_number
from worlds.cvaos.ram import AoSRAM
from worlds.cvaos.ram.addresses import EWRAM
from worlds.cvaos.rom import received_item_box as box

from .test_ram_map import FakeBackend

ROM = announce.ROM_DOMAIN
MB = announce.MAILBOX_OFFSET


def rom_with_names(**text_ids: int) -> bytearray:
    """A ROM-domain buffer whose name table gives ``text_ids`` (keyed by ``g<global-id>``)."""
    rom = bytearray(0x800000)
    for key, text_id in text_ids.items():
        gid = int(key[1:])
        at = announce.NAME_TABLE_OFFSET + gid * 2
        rom[at:at + 2] = text_id.to_bytes(2, "little")
    return rom


class AnnounceTest(unittest.TestCase):
    def setUp(self) -> None:
        announce._text_id_cache.clear()
        self.ewram = bytearray(0x40000)
        self.rom = rom_with_names(g1=92, g200=700)
        self.ram = AoSRAM(FakeBackend({EWRAM: self.ewram, ROM: self.rom}))

    def run_(self, coro):
        return asyncio.run(coro)

    def mailbox(self) -> bytes:
        return bytes(self.ewram[MB:MB + box.MAILBOX_SIZE])

    # --- the happy paths ---------------------------------------------------
    def test_item_posts_the_textbox_request(self):
        self.assertTrue(self.run_(announce.announce_item_number(self.ram, 1)))
        self.assertEqual(self.mailbox(), box.mailbox_write(92))
        self.assertEqual(self.mailbox()[box.MB_KIND], box.KIND_ITEM)
        self.assertEqual(self.mailbox()[box.MB_PENDING], 1)

    def test_soul_posts_the_soul_banner_request(self):
        info = by_item_number[200]                       # a blue_soul
        self.assertEqual(info.item_category, "blue_soul")
        # the grant has already happened by the time we announce: count 1 == first one
        self.run_(self.ram.give_item(info.item_category, info.id))
        self.assertTrue(self.run_(announce.announce_item_number(self.ram, 200)))
        self.assertEqual(
            self.mailbox(),
            box.soul_mailbox_write(soul_index=info.id, soul_type=1, is_new=True),
        )
        self.assertEqual(self.mailbox()[box.MB_KIND], box.KIND_SOUL)

    def test_a_duplicate_soul_announces_as_a_duplicate(self):
        info = by_item_number[200]
        for _ in range(2):                               # owned count reaches 2
            self.run_(self.ram.give_item(info.item_category, info.id))
        self.assertEqual(self.run_(self.ram.owned_count(info.item_category, info.id)), 2)
        self.assertTrue(self.run_(announce.announce_item_number(self.ram, 200)))
        self.assertEqual(self.mailbox()[box.MB_ARG2], 0, "isNew must be 0 for a duplicate")

    def test_every_soul_category_maps_to_a_soul_type(self):
        cats = {i.item_category for i in by_item_number.values() if i.item_category.endswith("_soul")}
        self.assertEqual(cats, set(announce.SOUL_TYPE_BY_CATEGORY))

    def test_text_id_is_read_once_and_cached(self):
        reads = []
        inner = self.ram.backend.read_many

        async def counting(requests):
            reads.extend(r for r in requests if r[2] == ROM)
            return await inner(requests)

        self.ram.backend.read_many = counting
        self.run_(announce.announce_item_number(self.ram, 1))
        self.ewram[MB + box.MB_PENDING] = 0              # pretend the ROM consumed it
        self.run_(announce.announce_item_number(self.ram, 1))
        self.assertEqual(len(reads), 1, "the name table should be read once per item")

    # --- getting out of the way -------------------------------------------
    def test_skips_while_the_previous_request_is_pending(self):
        self.assertTrue(self.run_(announce.announce_item_number(self.ram, 1)))
        before = self.mailbox()
        self.assertFalse(self.run_(announce.announce_item_number(self.ram, 200)))
        self.assertEqual(self.mailbox(), before, "must not overwrite an unconsumed request")

    def test_announces_again_once_the_rom_consumes_the_request(self):
        self.run_(announce.announce_item_number(self.ram, 1))
        self.ewram[MB + box.MB_PENDING] = 0              # the hook fired
        self.assertTrue(self.run_(announce.announce_item_number(self.ram, 200)))
        self.assertEqual(self.mailbox()[box.MB_KIND], box.KIND_SOUL)

    def test_unknown_item_number_is_not_announced(self):
        self.assertFalse(self.run_(announce.announce_item_number(self.ram, 9999)))
        self.assertEqual(self.mailbox(), bytes(box.MAILBOX_SIZE))

    def test_item_with_no_name_text_id_is_not_announced(self):
        self.rom[announce.NAME_TABLE_OFFSET + 2:announce.NAME_TABLE_OFFSET + 4] = b"\x00\x00"
        self.assertFalse(self.run_(announce.announce_item_number(self.ram, 1)))
        self.assertEqual(self.mailbox(), bytes(box.MAILBOX_SIZE))

    def test_a_failing_backend_never_raises(self):
        async def boom(*_a, **_k):
            raise OSError("connection lost")

        self.ram.backend.read_many = boom
        self.assertFalse(self.run_(announce.announce_item_number(self.ram, 1)))


class GrantIntegrationTest(unittest.TestCase):
    """The announcement rides on a successful grant and never changes its outcome."""

    def setUp(self) -> None:
        announce._text_id_cache.clear()
        self.ewram = bytearray(0x40000)
        self.ram = AoSRAM(FakeBackend({EWRAM: self.ewram, ROM: rom_with_names(g1=92)}))

    def run_(self, coro):
        return asyncio.run(coro)

    def test_successful_grant_announces(self):
        from worlds.cvaos import item_granting
        action = item_granting.ReceiveAction(
            category=item_granting.TransferCategory.PICKUP,
            id_or_value=1, set_flag=False, flag_offset=0, flag_bit=0, flag_value=0,
        )
        self.assertTrue(self.run_(item_granting.grant(self.ram, action)))
        self.assertEqual(bytes(self.ewram[MB:MB + box.MAILBOX_SIZE]), box.mailbox_write(92))

    def test_a_lost_grant_does_not_announce(self):
        from worlds.cvaos import item_granting
        action = item_granting.ReceiveAction(
            category=item_granting.TransferCategory.PICKUP,
            id_or_value=1, set_flag=False, flag_offset=0, flag_bit=0, flag_value=0,
        )

        async def lose(*_a, **_k):
            return False

        self.ram.guarded_write = lose
        self.run_(item_granting.grant(self.ram, action))
        self.assertEqual(bytes(self.ewram[MB:MB + box.MAILBOX_SIZE]), bytes(box.MAILBOX_SIZE),
                         "a grant that lost its guarded write must not announce")


if __name__ == "__main__":
    unittest.main()
