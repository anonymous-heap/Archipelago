"""A save from another seed sends no checks (client_logic._save_from_another_seed).

Only this seed's items advance the saved received-item counter, so a counter ahead of every item
the server has sent marks a save from another seed. New Game+ keeps that counter. A real save's
counter is also ahead for a moment after connecting, until the server's items arrive, so the
guard holds checks then but warns only once the item list is known to be complete.

These tests drive the shared logic with a fake context and RAM, so they cover both clients. The
last class checks that each client passes the server's packets to that logic.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from NetUtils import NetworkItem

from ..client_logic import ITEMS_FROM_OTHER_WORLDS, RECEIVED_ITEMS_PROBE, CVAOSClientLogic
from ..items import item_table

PROBE = [{"cmd": "Get", "keys": [], RECEIVED_ITEMS_PROBE: True}]
PROBE_ANSWER = {"keys": {}, RECEIVED_ITEMS_PROBE: True}


class FakeRam:
    def __init__(self, received: int) -> None:
        self.received = received

    async def get_received_count(self) -> int:
        return self.received


def connection(items: int) -> SimpleNamespace:
    """A connected context whose server has sent ``items`` items from another world."""
    item = NetworkItem(item_table["Potion"].code, 5, 2)
    return SimpleNamespace(server=object(), slot=1, slot_data={},
                           items_handling=ITEMS_FROM_OTHER_WORLDS, items_received=[item] * items,
                           send_msgs=mock.AsyncMock())


class SaveGuardTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = CVAOSClientLogic()
        self.client._reset_session_state()

    async def held(self, ctx: SimpleNamespace, received: int) -> bool:
        return await self.client._save_from_another_seed(ctx, FakeRam(received))

    def answer_probe(self, ctx: SimpleNamespace) -> None:
        self.client.track_received_items(ctx, "Retrieved", PROBE_ANSWER)

    async def test_a_brand_new_save_sends_its_checks_at_once(self) -> None:
        ctx = connection(items=0)
        self.assertFalse(await self.held(ctx, received=0))
        ctx.send_msgs.assert_not_awaited()

    async def test_new_game_plus_from_another_seed_is_held_with_one_warning(self) -> None:
        # A finished seed's file counts 150 items, and this seed has sent 3.
        ctx = connection(items=3)
        with self.assertNoLogs("Client", level="INFO"):
            self.assertTrue(await self.held(ctx, received=150))
            self.assertTrue(await self.held(ctx, received=150))
        ctx.send_msgs.assert_awaited_once_with(PROBE)
        self.answer_probe(ctx)
        with self.assertLogs("Client", level="WARNING"):
            self.assertTrue(await self.held(ctx, received=150))
        with self.assertNoLogs("Client", level="INFO"):
            self.assertTrue(await self.held(ctx, received=150))

    async def test_items_arriving_after_the_first_read_release_the_checks_unwarned(self) -> None:
        # A reconnect, or a first connect, can tick before the server's items arrive.
        ctx = connection(items=0)
        with self.assertNoLogs("Client", level="INFO"):
            self.assertTrue(await self.held(ctx, received=3))
            ctx.items_received = connection(items=3).items_received
            self.answer_probe(ctx)
            self.assertFalse(await self.held(ctx, received=3))

    async def test_loading_this_seeds_save_lifts_the_hold(self) -> None:
        ctx = connection(items=3)
        await self.held(ctx, received=150)
        self.answer_probe(ctx)
        with self.assertLogs("Client", level="WARNING"):
            await self.held(ctx, received=150)
        with self.assertLogs("Client", level="INFO"):
            self.assertFalse(await self.held(ctx, received=2))

    async def test_a_new_connection_needs_its_own_answer(self) -> None:
        ctx = connection(items=3)
        await self.held(ctx, received=150)
        self.answer_probe(ctx)
        ctx.server, ctx.items_received = object(), []   # reconnected; the items are not back yet
        ctx.send_msgs.reset_mock()
        with self.assertNoLogs("Client", level="INFO"):
            self.assertTrue(await self.held(ctx, received=3))
        ctx.send_msgs.assert_awaited_once_with(PROBE)

    async def test_items_the_client_discarded_need_a_new_answer(self) -> None:
        # CommonClient discards a ReceivedItems packet that does not follow on from its list, and
        # asks the server for every item again.
        ctx = connection(items=3)
        await self.held(ctx, received=5)
        self.answer_probe(ctx)
        self.client.track_received_items(ctx, "ReceivedItems", {"index": 4, "items": [[1, 2, 3]]})
        ctx.send_msgs.reset_mock()
        with self.assertNoLogs("Client", level="INFO"):
            self.assertTrue(await self.held(ctx, received=5))
        ctx.send_msgs.assert_awaited_once_with(PROBE)


class ClientWiringTest(unittest.IsolatedAsyncioTestCase):
    """Each client passes the probe's answer to the shared logic."""

    async def test_bizhawk(self) -> None:
        from ..client import CVAOSClient
        client = CVAOSClient()
        ctx = connection(items=0)
        client.on_package(ctx, "Retrieved", PROBE_ANSWER)
        self.assertIs(client.items_complete_on, ctx.server)

    async def test_collection(self) -> None:
        from ..collection_client import CVAOSCollectionContext
        ctx = CVAOSCollectionContext(None, None)
        ctx.server = object()
        ctx.on_package("Retrieved", PROBE_ANSWER)
        self.assertIs(ctx.brain.items_complete_on, ctx.server)


if __name__ == "__main__":
    unittest.main()
