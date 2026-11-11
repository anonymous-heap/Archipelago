"""The save guard holds back a save's checks while it is ahead (client_logic._checks_held).

A save whose received-item counter is ahead of every item the server has sent may belong to
another seed, and New Game+ keeps that counter. A server that lost its most recent checks also
leaves the player's own save ahead, because the counter counts own items that the server sends.
The player can then release the hold with /trust_save. A real save's counter is also ahead for a
moment after connecting, until the server's items arrive, so the guard holds checks then but
warns only once the item list is known to be complete.

These tests drive the shared logic with a fake context and RAM, so they cover both clients. The
last class checks that each client passes the server's packets and the command to that logic.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from NetUtils import ClientStatus, NetworkItem

from ..client_logic import (ITEMS_FROM_OTHER_WORLDS, ITEMS_INCLUDING_OWN, RECEIVED_ITEMS_PROBE,
                            CVAOSClientLogic)
from ..data import enemy_type_by_name
from ..enemy_checks import defeat_location_id
from ..items import item_table
from ..options import Goal
from ..ram import AoSRAM, addresses as addr
from ..ram.addresses import EWRAM, ROM
from .test_ram_map import FakeBackend

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
        return await self.client._checks_held(ctx, FakeRam(received))

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

    async def test_trust_save_without_a_warning_releases_nothing(self) -> None:
        # Before the warning, a hold may only be waiting for the server's items.
        ctx = connection(items=0)
        self.assertTrue(await self.held(ctx, received=3))
        with self.assertLogs("Client", level="INFO"):
            self.client.trust_save()
        self.assertFalse(self.client.save_trusted)


BAT = enemy_type_by_name["Bat"].id
ZOMBIE = enemy_type_by_name["Zombie"].id
SKELETON = enemy_type_by_name["Skeleton"].id
POTION = item_table["Potion"].code


def offset(entry) -> int:
    return entry.request().offset


class ServerRollbackTest(unittest.IsolatedAsyncioTestCase):
    """
    A server that lost its most recent checks leaves the player's own save ahead.

    The save has defeated a Bat and a Zombie and beaten Graham. Its counter is 1, because the
    server had sent the own Potion placed at the Bat's check, and the client granted it. The
    server then restarted from an autosave older than that check, so it has sent nothing. These
    tests drive whole ticks over the real RAM map, and the guard holds every check, including the
    Bat's, which would make the server send the Potion again.
    """

    def setUp(self) -> None:
        self.ewram = bytearray(0x40000)
        self.ewram[offset(addr.GAME_STATE)] = int(addr.GameState.INGAME)
        self.ewram[offset(addr.MENU_STATE)] = addr.MENU_STATE_NORMAL
        for vital in (addr.MAX_HP, addr.CURRENT_HP):
            self.ewram[offset(vital):offset(vital) + 2] = (320).to_bytes(2, "little")
        self.ewram[offset(addr.BOSS_FLAGS)] = addr.BOSS_FLAG_GRAHAM
        self.set_counter(1)
        self.defeat(BAT, ZOMBIE)
        self.ram = AoSRAM(FakeBackend({EWRAM: self.ewram, ROM: bytearray(0x800000)}))
        self.client = CVAOSClientLogic()
        self.client._reset_session_state()

        async def update_death_link(_) -> None:
            pass

        locations = {defeat_location_id(enemy) for enemy in (BAT, ZOMBIE, SKELETON)}
        self.ctx = SimpleNamespace(
            server=object(), slot=1, team=0, tags=set(), finished_game=False,
            player_names={1: "Soma"}, items_handling=ITEMS_INCLUDING_OWN,
            slot_data={"rom_given_locations": [], "starting_inventory_from_server": True,
                       "goal": Goal.option_graham},
            server_locations=locations, checked_locations=set(), items_received=[],
            send_msgs=mock.AsyncMock(), update_death_link=update_death_link)

    def set_counter(self, count: int) -> None:
        counter = offset(addr.AP_RECEIVED_COUNT)
        self.ewram[counter:counter + 2] = count.to_bytes(2, "little")

    def defeat(self, *enemies: int) -> None:
        base = offset(addr.ENEMY_DEFEATED)
        for enemy in enemies:
            self.ewram[base + (enemy >> 3)] |= 1 << (enemy & 7)

    async def tick(self) -> list[dict]:
        """Run one tick, and return the checks, goal and probe it sent."""
        self.ctx.send_msgs.reset_mock()
        await self.client._tick(self.ctx, self.ram)
        return [message for call in self.ctx.send_msgs.await_args_list for message in call.args[0]
                if message["cmd"] in ("LocationChecks", "StatusUpdate", "Get")]

    async def tick_until_warned(self) -> list[dict]:
        """Tick until the server answers the probe and the guard warns, and return what was sent."""
        sent = await self.tick()
        self.client.track_received_items(self.ctx, "Retrieved", PROBE_ANSWER)
        with self.assertLogs("Client", level="WARNING") as logs:
            sent += await self.tick()
        self.assertIn("/trust_save", logs.output[-1])
        return sent

    async def test_the_save_is_held_until_trust_save_sends_its_checks_and_goal(self) -> None:
        self.assertEqual(await self.tick_until_warned(), PROBE)
        self.client.trust_save()
        self.assertEqual(await self.tick(), [
            {"cmd": "LocationChecks", "locations": sorted([defeat_location_id(BAT),
                                                           defeat_location_id(ZOMBIE)])},
            {"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}])

    async def test_a_reconnect_holds_the_save_again(self) -> None:
        await self.tick_until_warned()
        self.client.trust_save()
        await self.tick()
        # The checks went into a connection that died, so the new one has sent nothing either.
        self.client._reset_session_state(same_seed=True)
        self.ctx.server = object()
        self.assertEqual(await self.tick_until_warned(), PROBE)

    async def test_the_release_ends_once_the_save_catches_up(self) -> None:
        await self.tick_until_warned()
        self.client.trust_save()
        await self.tick()
        # The server answers the Bat's check with the Potion, so the save is no longer ahead.
        self.ctx.items_received = [NetworkItem(POTION, defeat_location_id(BAT), 1)]
        await self.tick()
        self.assertFalse(self.client.save_trusted)
        # Another seed's save loaded afterwards is held, and warned about at once, because the
        # probe was already answered on this connection.
        self.set_counter(150)
        self.defeat(SKELETON)
        with self.assertLogs("Client", level="WARNING"):
            self.assertEqual(await self.tick(), [])


class ClientWiringTest(unittest.IsolatedAsyncioTestCase):
    """Each client passes the probe's answer and /trust_save to the shared logic."""

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

    async def test_bizhawk_trust_save(self) -> None:
        from ..client import CVAOSClient, cmd_trust_save
        client = CVAOSClient()
        ctx = mock.Mock()
        ctx.command_processor.commands = {}
        with mock.patch.object(CVAOSClient, "_validate_rom", mock.AsyncMock(return_value=True)):
            await client.validate_rom(ctx)
        self.assertIs(ctx.command_processor.commands["trust_save"], cmd_trust_save)
        ctx.client_handler = client
        client.warned_save_ahead = True
        cmd_trust_save(SimpleNamespace(ctx=ctx))
        self.assertTrue(client.save_trusted)

    async def test_collection_trust_save(self) -> None:
        from ..collection_client import CVAOSCollectionContext
        ctx = CVAOSCollectionContext(None, None)
        ctx.brain.warned_save_ahead = True
        ctx.command_processor(ctx)("/trust_save")
        self.assertTrue(ctx.brain.save_trusted)


if __name__ == "__main__":
    unittest.main()
