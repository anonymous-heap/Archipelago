"""Tests for announcing a received item in game (announce.py).

The announcement is cosmetic, so these check both that it posts the right request and that it
gets out of the way when it cannot: a mailbox the ROM has not drained yet, an item with no
banner, and a backend that fails outright.
"""
from __future__ import annotations

import unittest

from worlds.cvaos import announce, item_granting
from worlds.cvaos.data.item_info import by_item_number
from worlds.cvaos.items import FORBIDDEN_AREA_SWITCH, item_table
from worlds.cvaos.ram import AoSRAM
from worlds.cvaos.ram.addresses import EWRAM, ROM
from worlds.cvaos.rom import custom_pickups as cp
from worlds.cvaos.rom import received_item_box as box
from worlds.cvaos.rom.custom_pickup_content import STUDY_SEALSWITCH

from .test_ram_map import FakeBackend
from . import find_rom

MB = announce.MAILBOX_OFFSET


def rom_with_names(**text_ids: int) -> bytearray:
    """A ROM-domain buffer whose name table gives ``text_ids`` (keyed by ``g<global-id>``)."""
    rom = bytearray(0x800000)
    for key, text_id in text_ids.items():
        gid = int(key[1:])
        at = announce.NAME_TABLE_OFFSET + gid * 2
        rom[at:at + 2] = text_id.to_bytes(2, "little")
    return rom


def add_gold_rows(rom: bytearray, rows: dict[int, int]) -> bytearray:
    """Fill the gold table of ``rom`` with ``rows``, each an amount and its text-id."""
    for index, (amount, text_id) in enumerate(rows.items()):
        at = announce.GOLD_TABLE_OFFSET + index * box.GOLD_ROW_SIZE
        rom[at:at + 2] = amount.to_bytes(2, "little")
        rom[at + 4:at + 8] = text_id.to_bytes(4, "little")
    return rom


class AnnounceCase(unittest.IsolatedAsyncioTestCase):
    """Runs announce.py over a fake backend, with empty caches and an empty mailbox.

    ``self.rom`` is the ROM domain, built by :meth:`base_rom`, and ``self.ewram`` holds the
    mailbox.
    """

    def base_rom(self) -> bytearray:
        return bytearray(0x800000)

    def setUp(self) -> None:
        announce._text_id_cache.clear()
        announce._gold_text_ids.clear()
        self.ewram = bytearray(0x40000)
        self.rom = self.base_rom()
        self.ram = AoSRAM(FakeBackend({EWRAM: self.ewram, ROM: self.rom}))

    def mailbox(self) -> bytes:
        return bytes(self.ewram[MB:MB + box.MAILBOX_SIZE])


class AnnounceTest(AnnounceCase):
    def base_rom(self) -> bytearray:
        return rom_with_names(g1=92, g200=700)

    # --- the happy paths ---------------------------------------------------
    async def test_item_posts_the_textbox_request(self):
        self.assertTrue(await announce.announce_item_number(self.ram, 1))
        self.assertEqual(self.mailbox(), box.mailbox_write(92))

    async def test_equipment_plays_the_equipment_sound(self):
        # Vanilla plays 0xB7 for weapons and armor (accessories included), and 0xB5 for consumables.
        self.rom[:] = rom_with_names(g32=40, g92=41, g116=42)
        for number, category in ((32, "weapon"), (92, "armor"), (116, "accessory")):
            with self.subTest(category=category):
                self.assertEqual(by_item_number[number].item_category, category)
                self.ewram[MB + box.MB_PENDING] = 0
                self.assertTrue(await announce.announce_item_number(self.ram, number))
                sfx = int.from_bytes(self.mailbox()[box.MB_SFX:box.MB_SFX + 2], "little")
                self.assertEqual(sfx, box.SFX_ITEM_TO_INVENTORY)

    async def test_soul_posts_the_soul_banner_request(self):
        info = by_item_number[200]                       # a blue_soul
        self.assertEqual(info.item_category, "blue_soul")
        # the grant has already happened by the time we announce: count 1 == first one
        await self.ram.give_item(info.item_category, info.id)
        self.assertTrue(await announce.announce_item_number(self.ram, 200))
        self.assertEqual(
            self.mailbox(),
            box.soul_mailbox_write(soul_index=info.id, soul_type=1, is_new=True),
        )

    async def test_new_soul_pause_off_announces_a_first_soul_as_a_duplicate(self):
        info = by_item_number[200]
        await self.ram.give_item(info.item_category, info.id)      # owned count is now 1
        self.assertTrue(await announce.announce_item_number(self.ram, 200,
                                                            new_soul_pause=False))
        self.assertEqual(self.mailbox()[box.MB_ARG2], 0,
                         "with the pause off a first soul must post isNew=0")

    async def test_a_duplicate_soul_announces_as_a_duplicate(self):
        info = by_item_number[200]
        for _ in range(2):                               # owned count reaches 2
            await self.ram.give_item(info.item_category, info.id)
        self.assertEqual(await self.ram.owned_count(info.item_category, info.id), 2)
        self.assertTrue(await announce.announce_item_number(self.ram, 200))
        self.assertEqual(self.mailbox()[box.MB_ARG2], 0, "isNew must be 0 for a duplicate")

    def test_every_soul_category_maps_to_a_soul_type(self):
        cats = {i.item_category for i in by_item_number.values() if i.item_category.endswith("_soul")}
        self.assertEqual(cats, set(announce.SOUL_TYPE_BY_CATEGORY))

    async def test_text_id_is_read_once_and_cached(self):
        await announce.announce_item_number(self.ram, 1)
        self.ewram[MB + box.MB_PENDING] = 0              # pretend the ROM consumed it
        await announce.announce_item_number(self.ram, 1)
        rom_reads = [request for batch in self.ram.backend.read_batches for request in batch
                     if request[2] == ROM]
        self.assertEqual(len(rom_reads), 1, "the name table should be read once per item")

    # --- getting out of the way -------------------------------------------
    async def test_skips_while_the_previous_request_is_pending(self):
        self.assertTrue(await announce.announce_item_number(self.ram, 1))
        before = self.mailbox()
        self.assertFalse(await announce.announce_item_number(self.ram, 200))
        self.assertEqual(self.mailbox(), before, "must not overwrite an unconsumed request")

    async def test_announces_again_once_the_rom_consumes_the_request(self):
        await announce.announce_item_number(self.ram, 1)
        self.ewram[MB + box.MB_PENDING] = 0              # the hook fired
        self.assertTrue(await announce.announce_item_number(self.ram, 200))
        self.assertEqual(self.mailbox()[box.MB_KIND], box.KIND_SOUL)

    async def test_unknown_item_number_is_not_announced(self):
        self.assertFalse(await announce.announce_item_number(self.ram, 9999))
        self.assertEqual(self.mailbox(), bytes(box.MAILBOX_SIZE))

    async def test_item_with_no_name_text_id_is_not_announced(self):
        self.rom[announce.NAME_TABLE_OFFSET + 2:announce.NAME_TABLE_OFFSET + 4] = b"\x00\x00"
        self.assertFalse(await announce.announce_item_number(self.ram, 1))
        self.assertEqual(self.mailbox(), bytes(box.MAILBOX_SIZE))

    async def test_a_failing_backend_never_raises(self):
        async def boom(*_a, **_k):
            raise OSError("connection lost")

        self.ram.backend.read_many = boom
        self.assertFalse(await announce.announce_item_number(self.ram, 1))


class FlagOnlyAnnounceTest(AnnounceCase):
    """A flag-only item is announced under the custom pickup that sets the same flag."""

    async def test_a_flag_no_pickup_claims_is_not_announced(self):
        self.assertFalse(await announce.announce_flag(self.ram, 0x34A, 5))
        self.assertEqual(self.mailbox(), bytes(box.MAILBOX_SIZE))

    async def test_skips_while_the_previous_request_is_pending(self):
        self.ewram[MB + box.MB_PENDING] = 1
        flag_offset, flag_bit = cp.flag_location(STUDY_SEALSWITCH)
        self.assertFalse(await announce.announce_flag(self.ram, flag_offset, flag_bit))


class AnnounceGoldTest(AnnounceCase):
    def base_rom(self) -> bytearray:
        return add_gold_rows(bytearray(0x800000), {1: 835, 100: 838, 2000: 841})

    async def test_gold_posts_the_amount_textbox_and_the_gold_sound(self):
        self.assertTrue(await announce.announce_gold(self.ram, 100))
        self.assertEqual(self.mailbox(), box.mailbox_write(838, box.SFX_MONEY))

    async def test_an_amount_no_gold_pickup_holds_is_not_announced(self):
        self.assertFalse(await announce.announce_gold(self.ram, 200))
        self.assertEqual(self.mailbox(), bytes(box.MAILBOX_SIZE))

    async def test_skips_while_the_previous_request_is_pending(self):
        self.ewram[MB + box.MB_PENDING] = 1
        self.assertFalse(await announce.announce_gold(self.ram, 100))

    async def test_a_failing_backend_never_raises(self):
        async def boom(*_a, **_k):
            raise OSError("connection lost")

        self.ram.backend.read_many = boom
        self.assertFalse(await announce.announce_gold(self.ram, 100))

    async def test_every_gold_item_has_a_row_in_the_real_rom(self):
        path = find_rom()
        if path is None:
            self.skipTest("base ROM not found (set CVAOS_USA_ROM)")
        rom = bytearray(open(path, "rb").read())
        ram = AoSRAM(FakeBackend({EWRAM: self.ewram, ROM: rom}))
        amounts = {action.id_or_value for action in map(item_granting.resolve,
                                                        (data.code for data in item_table.values()))
                   if action.category == item_granting.TransferCategory.MONEY}
        self.assertEqual({amount for amount in amounts
                          if await announce._gold_text_id(ram, amount) is None}, set())


class GrantIntegrationTest(AnnounceCase):
    """The announcement rides on a successful grant and never changes its outcome."""

    def base_rom(self) -> bytearray:
        return rom_with_names(g1=92)

    async def test_successful_grant_announces(self):
        # High Potion is global-id 1, which base_rom names with text-id 92.
        action = item_granting.resolve(item_table["High Potion"].code)
        self.assertTrue(await item_granting.grant(self.ram, action))
        self.assertEqual(self.mailbox(), box.mailbox_write(92))

    async def test_a_gold_grant_announces_the_amount(self):
        add_gold_rows(self.rom, {100: 838})
        action = item_granting.resolve(item_table["100 Gold"].code)
        self.assertTrue(await item_granting.grant(self.ram, action))
        self.assertEqual(self.mailbox(), box.mailbox_write(838, box.SFX_MONEY))

    async def test_a_lost_grant_does_not_announce(self):
        action = item_granting.resolve(item_table["High Potion"].code)

        async def lose(*_a, **_k):
            return False

        self.ram.guarded_write_many = lose
        self.assertFalse(await item_granting.grant(self.ram, action))
        self.assertEqual(self.mailbox(), bytes(box.MAILBOX_SIZE),
                         "a grant that lost its guarded write must not announce")

    async def test_a_flag_only_grant_sets_the_flag_and_announces_the_pickup(self):
        action = item_granting.resolve(item_table[FORBIDDEN_AREA_SWITCH].code)
        self.assertTrue(await item_granting.grant(self.ram, action))
        self.assertEqual((self.ewram[action.flag_offset] >> action.flag_bit) & 1, 1)
        self.assertEqual(self.mailbox(),
                         box.mailbox_write(cp.STRINGPTR_COUNT, STUDY_SEALSWITCH.sfx))


if __name__ == "__main__":
    unittest.main()
