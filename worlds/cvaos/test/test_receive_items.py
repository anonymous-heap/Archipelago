"""_receive_items grants each received item and advances the saved received-counter in one step.

An item's transfer and the counter's advance go in one guarded write. If they were two requests, a
failure between them would leave the item granted and the counter behind it. The next tick would
then grant the item again. A connector timeout while the emulator is paused is one such failure.
A flag-only item sets its flag in a write of its own first, which is safe to repeat.

These tests drive _receive_items over the dict-backed FakeBackend, so they need no emulator.
"""
from __future__ import annotations

import contextlib
import unittest
from types import SimpleNamespace

from NetUtils import NetworkItem

from .. import announce, item_granting
from ..client_logic import (ITEMS_FROM_OTHER_WORLDS, MAX_ITEMS_PER_TICK, STARTING_LOCATION,
                            CVAOSClientLogic)
from ..items import FORBIDDEN_AREA_SWITCH, PROGRESSIVE_MOVEMENT, item_table
from ..ram import AoSRAM, addresses as addr
from ..ram.addresses import EWRAM
from ..rom import received_item_box as box
from .test_ram_map import FakeBackend

ROM = "ROM"
COUNTER_OFFSET = addr.AP_RECEIVED_COUNT.request().offset
GOLD_OFFSET = addr.CURRENT_GOLD.request().offset
SOULS_COLLECTED_OFFSET = addr.SOULS_COLLECTED.request().offset
MIND_UP_OFFSET = addr.INVENTORY["consumable"].entry.item(5).request().offset
FLYING_ARMOR_OFFSET = addr.INVENTORY["blue_soul"].entry.item(0).request().offset  # even nibble
MAILBOX = slice(announce.MAILBOX_OFFSET, announce.MAILBOX_OFFSET + box.MAILBOX_SIZE)


class TimesOutAfterAWrite(FakeBackend):
    """A backend whose requests fail after its first successful guarded write, until ``heal()`` is
    called.

    A connector that times out can look like this to the client: the write landed, and the
    requests after it raise.
    """

    def __init__(self, memory: dict) -> None:
        super().__init__(memory)
        self.armed = True
        self.failing = False

    def heal(self) -> None:
        self.armed = self.failing = False

    def _check(self) -> None:
        if self.failing:
            raise ConnectionError("connector timed out")

    async def read_many(self, requests):
        self._check()
        return await super().read_many(requests)

    async def write(self, offset, data, domain):
        self._check()
        await super().write(offset, data, domain)

    async def guarded_write(self, offset, data, expected, domain):
        self._check()
        ok = await super().guarded_write(offset, data, expected, domain)
        self.failing = ok and self.armed
        return ok

    async def guarded_write_many(self, writes):
        self._check()
        ok = await super().guarded_write_many(writes)
        self.failing = ok and self.armed
        return ok


class CounterMoves(FakeBackend):
    """A backend where another writer moves the received-counter after the client read it."""

    async def guarded_write_many(self, writes):
        self.memory[EWRAM][COUNTER_OFFSET] = 7
        return await super().guarded_write_many(writes)


class ReceiveItemsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.ewram = bytearray(0x40000)
        self.memory = {EWRAM: self.ewram, ROM: bytearray(0x800000)}
        self.ram = AoSRAM(FakeBackend(self.memory))
        self.client = CVAOSClientLogic()
        self.client._reset_session_state()

    @staticmethod
    def ctx(*names: str, codes: tuple[int, ...] = ()) -> SimpleNamespace:
        codes = (*codes, *(item_table[name].code for name in names))
        return SimpleNamespace(items_received=[NetworkItem(code, 0, 2) for code in codes],
                               slot_data={}, items_handling=ITEMS_FROM_OTHER_WORLDS)

    def gold(self) -> int:
        return int.from_bytes(self.ewram[GOLD_OFFSET:GOLD_OFFSET + 4], "little")

    async def fail_then_heal(self, name: str) -> AoSRAM:
        """Receive ``name`` over a connector that times out after the write, then receive it
        again once the connector recovers."""
        backend = TimesOutAfterAWrite(self.memory)
        ram = AoSRAM(backend)
        with contextlib.suppress(ConnectionError):
            await self.client._receive_items(self.ctx(name), ram)
        backend.heal()
        await self.client._receive_items(self.ctx(name), ram)
        return ram

    async def test_each_item_and_the_counter_advance(self) -> None:
        await self.client._receive_items(self.ctx("Mind Up", "Flying Armor", "500 Gold"),
                                         self.ram)
        self.assertEqual(await self.ram.owned_count("consumable", 5), 1)
        self.assertEqual(await self.ram.owned_count("blue_soul", 0), 1)
        self.assertEqual(self.gold(), 500)
        self.assertEqual(await self.ram.get_received_count(), 3)

    async def test_a_failure_after_the_grant_does_not_grant_an_item_again(self) -> None:
        ram = await self.fail_then_heal("Mind Up")
        self.assertEqual(await ram.owned_count("consumable", 5), 1,
                         "the item must be granted exactly once")
        self.assertEqual(await ram.get_received_count(), 1)

    async def test_a_failure_after_the_grant_does_not_grant_a_soul_again(self) -> None:
        ram = await self.fail_then_heal("Flying Armor")
        self.assertEqual(await ram.owned_count("blue_soul", 0), 1,
                         "the soul must be granted exactly once")
        self.assertEqual(await ram.get_received_count(), 1)

    async def test_a_failure_after_the_grant_does_not_add_gold_again(self) -> None:
        ram = await self.fail_then_heal("500 Gold")
        self.assertEqual(self.gold(), 500, "the gold must be added exactly once")
        self.assertEqual(await ram.get_received_count(), 1)

    async def test_a_failure_after_the_flag_write_still_delivers_a_flag_only_item(self) -> None:
        # The flag goes in its own write before the counter's batch. A timeout after that write
        # leaves the counter behind, so the next tick sets the flag again and the item is not lost.
        action = item_granting.resolve(item_table[FORBIDDEN_AREA_SWITCH].code)
        ram = await self.fail_then_heal(FORBIDDEN_AREA_SWITCH)
        self.assertEqual((self.ewram[action.flag_offset] >> action.flag_bit) & 1, 1)
        self.assertEqual(await ram.get_received_count(), 1)

    async def test_a_counter_changed_underneath_writes_nothing(self) -> None:
        ram = AoSRAM(CounterMoves(self.memory))
        await self.client._receive_items(self.ctx("Mind Up"), ram)
        self.assertEqual(await ram.owned_count("consumable", 5), 0)
        self.assertEqual(await ram.get_received_count(), 7)

    async def test_a_lost_soul_batch_does_not_count_the_soul(self) -> None:
        ram = AoSRAM(CounterMoves(self.memory))
        await self.client._receive_items(self.ctx("Flying Armor"), ram)
        self.assertEqual(await ram.owned_count("blue_soul", 0), 0)
        self.assertEqual(self.ewram[SOULS_COLLECTED_OFFSET], 0,
                         "the souls-collected statistic must not count a soul that did not land")

    async def test_a_lost_flag_only_batch_does_not_announce(self) -> None:
        ram = AoSRAM(CounterMoves(self.memory))
        await self.client._receive_items(self.ctx(FORBIDDEN_AREA_SWITCH), ram)
        self.assertEqual(bytes(self.ewram[MAILBOX]), bytes(box.MAILBOX_SIZE))

    async def test_a_consumable_at_its_cap_still_advances_the_counter(self) -> None:
        self.ewram[MIND_UP_OFFSET] = 9
        with self.assertLogs("Client", level="WARNING"):
            await self.client._receive_items(self.ctx("Mind Up"), self.ram)
        self.assertEqual(await self.ram.owned_count("consumable", 5), 9)
        self.assertEqual(await self.ram.get_received_count(), 1)

    async def test_a_soul_at_its_cap_still_advances_the_counter(self) -> None:
        self.ewram[FLYING_ARMOR_OFFSET] = 9
        with self.assertLogs("Client", level="WARNING"):
            await self.client._receive_items(self.ctx("Flying Armor"), self.ram)
        self.assertEqual(await self.ram.owned_count("blue_soul", 0), 9)
        self.assertEqual(await self.ram.get_received_count(), 1)

    async def test_an_item_at_its_cap_is_granted_if_the_player_uses_one_first(self) -> None:
        self.ewram[MIND_UP_OFFSET] = 9
        ewram = self.ewram

        class PlayerUsesOne(FakeBackend):
            async def guarded_write_many(self, writes):
                ewram[MIND_UP_OFFSET] = 8   # between the client's read and its write
                return await super().guarded_write_many(writes)

        ram = AoSRAM(PlayerUsesOne(self.memory))
        await self.client._receive_items(self.ctx("Mind Up"), ram)
        self.assertEqual(await self.ram.get_received_count(), 0, "a stale cap must fail the batch")
        await self.client._receive_items(self.ctx("Mind Up"), self.ram)
        self.assertEqual(await self.ram.owned_count("consumable", 5), 9)
        self.assertEqual(await self.ram.get_received_count(), 1)

    async def test_a_progressive_movement_copy_grants_nothing_and_later_items_arrive(self) -> None:
        # A copy's soul is granted from the client's count of copies, not when the copy is
        # received. Receiving one must still advance the saved received-counter past it, or every
        # later item would wait forever.
        await self.client._receive_items(self.ctx(PROGRESSIVE_MOVEMENT, "Mind Up"), self.ram)
        self.assertEqual(await self.ram.get_received_count(), 2)
        self.assertEqual(await self.ram.owned_count("consumable", 5), 1)
        self.assertEqual(await self.ram.owned_count("blue_soul", 0), 0,
                         "a copy grants no soul itself")

    async def test_a_flag_only_item_sets_its_flag_and_advances_the_counter(self) -> None:
        action = item_granting.resolve(item_table[FORBIDDEN_AREA_SWITCH].code)
        await self.client._receive_items(self.ctx(FORBIDDEN_AREA_SWITCH), self.ram)
        self.assertEqual((self.ewram[action.flag_offset] >> action.flag_bit) & 1, 1)
        self.assertEqual(await self.ram.get_received_count(), 1)

    async def test_an_unknown_code_is_skipped_and_the_next_item_still_arrives(self) -> None:
        with self.assertLogs("Client", level="WARNING"):
            await self.client._receive_items(self.ctx("Mind Up", codes=(1,)), self.ram)
        self.assertEqual(await self.ram.owned_count("consumable", 5), 1)
        self.assertEqual(await self.ram.get_received_count(), 2)

    async def test_a_reserved_category_is_skipped_and_the_next_item_still_arrives(self) -> None:
        reserved = item_granting.pack(item_granting.TransferCategory.EVENT, 0)
        with self.assertLogs("Client", level="WARNING"):
            await self.client._receive_items(self.ctx("Mind Up", codes=(reserved,)), self.ram)
        self.assertEqual(await self.ram.owned_count("consumable", 5), 1)
        self.assertEqual(await self.ram.get_received_count(), 2)

    def starting_then_received(self, slot_data: dict) -> SimpleNamespace:
        # The server sends the starting inventory first, at location -2.
        items = [NetworkItem(item_table["500 Gold"].code, STARTING_LOCATION, 2),
                 NetworkItem(item_table["Mind Up"].code, 5, 2)]
        return SimpleNamespace(items_received=items, slot_data=slot_data,
                               items_handling=ITEMS_FROM_OTHER_WORLDS)

    async def test_an_older_seeds_save_counts_only_items_from_other_worlds(self) -> None:
        # An older client already granted Mind Up and counted it, but never received the gold.
        self.ewram[MIND_UP_OFFSET] = 1
        await self.ram.set_received_count(1, expected=0)
        await self.client._receive_items(self.starting_then_received({}), self.ram)
        self.assertEqual(await self.ram.owned_count("consumable", 5), 1,
                         "Mind Up must not be granted again")
        self.assertEqual(self.gold(), 0)

    async def test_a_newer_seeds_save_counts_the_starting_inventory_too(self) -> None:
        ctx = self.starting_then_received({"starting_inventory_from_server": True})
        await self.client._receive_items(ctx, self.ram)
        self.assertEqual(self.gold(), 500)
        self.assertEqual(await self.ram.owned_count("consumable", 5), 1)
        self.assertEqual(await self.ram.get_received_count(), 2)

    async def test_one_tick_grants_at_most_max_items_per_tick(self) -> None:
        ctx = self.ctx(*["500 Gold"] * (MAX_ITEMS_PER_TICK + 5))
        await self.client._receive_items(ctx, self.ram)
        self.assertEqual(await self.ram.get_received_count(), MAX_ITEMS_PER_TICK)
        await self.client._receive_items(ctx, self.ram)
        self.assertEqual(await self.ram.get_received_count(), MAX_ITEMS_PER_TICK + 5)
        self.assertEqual(self.gold(), 500 * (MAX_ITEMS_PER_TICK + 5))


if __name__ == "__main__":
    unittest.main()
