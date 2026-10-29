"""
Tests for the cvaos DeathLink relay (client._relay_deathlink).

The relay keys off the engine's death STATE (MENU_STATE==DEATH during the in-game death fade, then
the GAME_OVER screen) rather than HP==0, because every death clamps HP to 0 and flips MENU_STATE to
the death sub-state on the same frame -- a state that persists for many frames, unlike the ~1-frame
(INGAME, NORMAL, hp==0) window the old check relied on and almost always missed.

These tests drive _relay_deathlink through the relevant transitions with a fake context and a fake
RAM accessor, so they need no emulator. Run from the Archipelago root:
    python -m pytest worlds/cvaos/test/test_deathlink.py -v
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import worlds._bizhawk as bizhawk

from ..client import CVAOSClient
from ..ram import addresses as addr

INGAME = int(addr.GameState.INGAME)
GAME_OVER = int(addr.GameState.GAME_OVER)
NORMAL = addr.MENU_STATE_NORMAL
DEATH = addr.MENU_STATE_DEATH
PAUSE = addr.MENU_STATE_PAUSE


class FakeCtx:
    """Minimal stand-in for BizHawkClientContext covering what _relay_deathlink touches."""

    def __init__(self, death_link: bool = True) -> None:
        self.slot_data = {"death_link": death_link}
        self.tags: set[str] = set()
        self.slot = 1
        self.player_names = {1: "Soma"}
        self.last_death_link = 1000.0
        self.sent_deaths: list[str] = []

    async def update_death_link(self, value: bool) -> None:
        if value:
            self.tags.add("DeathLink")
        else:
            self.tags.discard("DeathLink")

    async def send_death(self, text: str = "") -> None:
        self.sent_deaths.append(text)
        # AP stamps last_death_link when a death is sent; mimic a moving clock.
        self.last_death_link += 1.0


class FakeRam:
    """Fake AoSRAM: HP is whatever we set. request_kill sets the kill-request flag (the real ROM hook
    would consume it and run the death routine the next frame); it does NOT touch HP."""

    def __init__(self, hp: int = 100) -> None:
        self.hp = hp
        self.kill_calls = 0
        self.kill_request = False

    async def get_current_hp(self) -> int:
        return self.hp

    async def request_kill(self) -> None:
        self.kill_calls += 1
        self.kill_request = True

    async def get_kill_request(self) -> bool:
        return self.kill_request


def _new_client() -> CVAOSClient:
    # Bypass BizHawkClient.__init__ (registration / emulator wiring) and set just the per-session
    # DeathLink state. currently_dead starts False, as if the first tick had already read the game.
    client = CVAOSClient.__new__(CVAOSClient)
    client.death_causes = []
    client.currently_dead = False
    client.time_of_sent_death = None
    return client


class DeathLinkRelayTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = _new_client()
        self.ctx = FakeCtx(death_link=True)
        self.ram = FakeRam(hp=100)

    async def relay(self, game_state: int, menu_state: int, hp: int | None = None) -> None:
        if hp is not None:
            self.ram.hp = hp
        in_gameplay = game_state == INGAME and menu_state == NORMAL
        await self.client._relay_deathlink(self.ctx, self.ram, game_state, menu_state, in_gameplay)

    async def test_enables_death_link_from_slot_data(self) -> None:
        self.assertNotIn("DeathLink", self.ctx.tags)
        await self.relay(INGAME, NORMAL)
        self.assertIn("DeathLink", self.ctx.tags)

    async def test_does_not_enable_when_seed_disables_it(self) -> None:
        self.ctx.slot_data["death_link"] = False
        await self.relay(INGAME, NORMAL)
        self.assertNotIn("DeathLink", self.ctx.tags)

    async def test_broadcasts_once_on_death_substate(self) -> None:
        await self.relay(INGAME, NORMAL)            # alive: no death
        self.assertEqual(self.ctx.sent_deaths, [])
        await self.relay(INGAME, DEATH, hp=0)       # lethal hit -> death fade
        self.assertEqual(len(self.ctx.sent_deaths), 1)
        await self.relay(INGAME, DEATH, hp=0)       # still fading
        await self.relay(GAME_OVER, 0, hp=0)        # game-over screen
        self.assertEqual(len(self.ctx.sent_deaths), 1, "must not re-broadcast the same death")

    async def test_broadcasts_on_game_over_when_fade_window_missed(self) -> None:
        # Poll never samples the MENU_STATE==DEATH fade; first death-state it sees is GAME_OVER.
        await self.relay(INGAME, NORMAL)
        await self.relay(GAME_OVER, 0, hp=0)
        self.assertEqual(len(self.ctx.sent_deaths), 1)

    async def test_rearms_after_respawn_and_detects_second_death(self) -> None:
        await self.relay(INGAME, NORMAL)
        await self.relay(INGAME, DEATH, hp=0)
        self.assertEqual(len(self.ctx.sent_deaths), 1)
        await self.relay(INGAME, NORMAL, hp=100)    # respawn / continue: alive again
        self.assertFalse(self.client.currently_dead)
        await self.relay(INGAME, DEATH, hp=0)       # a second, independent death
        self.assertEqual(len(self.ctx.sent_deaths), 2)

    async def test_does_not_rearm_while_kill_pending(self) -> None:
        # The window right after we request an incoming kill: the hook hasn't run yet, so state is back
        # to NORMAL and HP is untouched, but the kill-request flag is still set. Re-arming here would
        # let the impending death echo back out as our own.
        self.client.currently_dead = True
        self.ram.kill_request = True
        await self.relay(INGAME, NORMAL, hp=100)
        self.assertTrue(self.client.currently_dead, "a pending kill must not count as a respawn")

    async def test_rearms_after_incoming_kill_consumed(self) -> None:
        # Once the hook has consumed the request (flag clear) and Soma is alive again, re-arm.
        self.client.currently_dead = True
        self.ram.kill_request = True
        await self.relay(INGAME, NORMAL, hp=100)        # pending -> no re-arm
        self.assertTrue(self.client.currently_dead)
        self.ram.kill_request = False                   # hook ran (death, then respawn)
        await self.relay(INGAME, NORMAL, hp=100)        # alive, nothing pending -> re-arm
        self.assertFalse(self.client.currently_dead)

    async def test_applies_incoming_death_during_gameplay(self) -> None:
        await self.relay(INGAME, NORMAL)            # enable tag
        self.client.death_causes.append("Someone else died")
        await self.relay(INGAME, NORMAL, hp=100)
        self.assertEqual(self.ram.kill_calls, 1)
        self.assertTrue(self.client.currently_dead)
        self.assertEqual(self.client.death_causes, [], "cause should be consumed")

    async def test_incoming_death_not_applied_outside_gameplay(self) -> None:
        await self.relay(INGAME, NORMAL)
        self.client.death_causes.append("Someone else died")
        await self.relay(INGAME, PAUSE)             # paused: unsafe to request the kill
        self.assertEqual(self.ram.kill_calls, 0)
        self.assertEqual(self.client.death_causes, ["Someone else died"], "cause stays queued")

    def bounce(self, cause: str) -> None:
        self.client.queue_deathlink_from_bounce(
            self.ctx, "Bounced", {"tags": ["DeathLink"], "data": {"time": 1.0, "cause": cause}})

    async def test_a_death_received_while_dead_is_dropped(self) -> None:
        await self.relay(INGAME, NORMAL)
        await self.relay(INGAME, DEATH, hp=0)       # Soma dies on his own
        self.bounce("Someone else died")
        self.assertEqual(self.client.death_causes, [])

    async def test_several_deaths_while_paused_kill_once(self) -> None:
        await self.relay(INGAME, NORMAL)
        for n in range(3):
            self.bounce(f"death {n}")
        await self.relay(INGAME, PAUSE)
        await self.relay(INGAME, NORMAL, hp=100)
        self.assertEqual(self.ram.kill_calls, 1)
        self.assertEqual(self.client.death_causes, [])

    async def test_dying_on_his_own_spends_a_pending_death(self) -> None:
        await self.relay(INGAME, NORMAL)
        self.bounce("Someone else died")
        await self.relay(INGAME, DEATH, hp=0)
        self.assertEqual(self.client.death_causes, [])

    # A reconnect to the same seed: set_auth runs the reset again and keeps the latch.
    def reconnect(self) -> None:
        self.client._reset_session_state(same_seed=True)

    # A first connect, or a different seed: the latch is unknown until a relay reads the game.
    def connect_fresh(self) -> None:
        self.client._reset_session_state()

    async def test_a_reconnect_does_not_send_the_same_death_again(self) -> None:
        await self.relay(INGAME, NORMAL)
        await self.relay(INGAME, DEATH, hp=0)
        self.assertEqual(len(self.ctx.sent_deaths), 1)
        self.reconnect()
        await self.relay(INGAME, DEATH, hp=0)
        await self.relay(GAME_OVER, 0, hp=0)
        self.assertEqual(len(self.ctx.sent_deaths), 1,
                         "a reconnect must not re-broadcast the death")
        await self.relay(INGAME, NORMAL, hp=100)    # continue: alive again, so the latch clears
        await self.relay(INGAME, DEATH, hp=0)
        self.assertEqual(len(self.ctx.sent_deaths), 2)

    async def test_a_death_during_the_disconnect_is_sent_after_reconnecting(self) -> None:
        await self.relay(INGAME, NORMAL)            # alive when the connection drops
        self.reconnect()
        await self.relay(GAME_OVER, 0, hp=0)        # he died while nothing could send it
        self.assertEqual(len(self.ctx.sent_deaths), 1)

    async def test_a_reconnect_with_a_kill_pending_does_not_broadcast_it(self) -> None:
        await self.relay(INGAME, NORMAL)
        self.client.death_causes.append("Someone else died")
        await self.relay(INGAME, NORMAL, hp=100)    # kill requested; the hook has not run yet
        self.reconnect()
        await self.relay(INGAME, NORMAL, hp=100)
        self.ram.kill_request = False               # the hook runs the death routine
        await self.relay(INGAME, DEATH, hp=0)
        self.assertEqual(self.ctx.sent_deaths, [],
                         "a kill pending at the reconnect must not be broadcast as our own death")

    async def test_connecting_during_a_death_does_not_broadcast_it(self) -> None:
        self.connect_fresh()
        await self.relay(INGAME, DEATH, hp=0)
        await self.relay(GAME_OVER, 0, hp=0)
        self.assertEqual(self.ctx.sent_deaths, [], "the death began before the client connected")
        await self.relay(INGAME, NORMAL, hp=100)
        await self.relay(INGAME, DEATH, hp=0)
        self.assertEqual(len(self.ctx.sent_deaths), 1)

    async def test_a_kill_pending_at_a_fresh_connect_is_not_broadcast(self) -> None:
        self.ram.kill_request = True                # left by an earlier client on this seed
        self.connect_fresh()
        await self.relay(INGAME, NORMAL, hp=100)
        self.ram.kill_request = False
        await self.relay(INGAME, DEATH, hp=0)
        self.assertEqual(self.ctx.sent_deaths, [])

    async def test_the_first_death_after_a_fresh_connect_is_broadcast(self) -> None:
        self.connect_fresh()
        await self.relay(INGAME, NORMAL, hp=100)
        self.assertIs(self.client.currently_dead, False)
        await self.relay(INGAME, DEATH, hp=0)
        self.assertEqual(len(self.ctx.sent_deaths), 1)

    async def test_a_fresh_connect_while_paused_reads_soma_as_alive(self) -> None:
        self.connect_fresh()
        await self.relay(INGAME, PAUSE, hp=100)
        self.assertIs(self.client.currently_dead, False)

    async def test_a_queued_death_is_applied_if_soma_is_alive_at_the_first_read(self) -> None:
        self.connect_fresh()
        self.bounce("Someone else died")
        await self.relay(INGAME, NORMAL, hp=100)
        self.assertEqual(self.ram.kill_calls, 1)

    async def test_a_queued_death_is_dropped_if_soma_is_dead_at_the_first_read(self) -> None:
        self.connect_fresh()
        self.bounce("Someone else died")
        await self.relay(GAME_OVER, 0, hp=0)
        self.assertEqual(self.ctx.sent_deaths, [])
        await self.relay(INGAME, NORMAL, hp=100)
        self.assertEqual(self.ram.kill_calls, 0)

    async def test_a_queued_death_is_dropped_if_a_kill_is_pending_at_the_first_read(self) -> None:
        self.ram.kill_request = True
        self.connect_fresh()
        self.bounce("Someone else died")
        await self.relay(INGAME, NORMAL, hp=100)
        self.assertEqual(self.client.death_causes, [])
        self.assertEqual(self.ram.kill_calls, 0)

    async def test_a_seed_without_deathlink_drops_the_tag_a_previous_seed_left(self) -> None:
        self.ctx.tags.add("DeathLink")
        self.ctx.slot_data["death_link"] = False
        await self.relay(INGAME, NORMAL)
        self.assertNotIn("DeathLink", self.ctx.tags)
        await self.relay(INGAME, DEATH, hp=0)
        self.assertEqual(self.ctx.sent_deaths, [])

    async def test_a_reconnect_to_the_same_seed_keeps_a_pending_death(self) -> None:
        await self.relay(INGAME, NORMAL)
        self.bounce("Someone else died")
        await self.relay(INGAME, PAUSE)             # paused, so the kill waits
        self.reconnect()
        await self.relay(INGAME, NORMAL, hp=100)
        self.assertEqual(self.ram.kill_calls, 1)

    async def test_a_different_seed_drops_a_pending_death(self) -> None:
        await self.relay(INGAME, NORMAL)
        self.bounce("Someone else died")
        self.connect_fresh()
        await self.relay(INGAME, NORMAL, hp=100)
        self.assertEqual(self.ram.kill_calls, 0)

    async def test_received_death_is_not_rebroadcast(self) -> None:
        # Applying an incoming death must not later echo back out as our own broadcast.
        await self.relay(INGAME, NORMAL)
        self.client.death_causes.append("Someone else died")
        await self.relay(INGAME, NORMAL, hp=100)    # apply incoming -> currently_dead, kill requested
        self.assertEqual(self.ram.kill_calls, 1)
        sent_before = len(self.ctx.sent_deaths)
        await self.relay(INGAME, DEATH, hp=0)       # the game now enters its death fade
        self.assertEqual(len(self.ctx.sent_deaths), sent_before,
                         "a received death must not be rebroadcast as our own")


class SetAuthTest(unittest.IsolatedAsyncioTestCase):
    """set_auth runs on every connect, and keeps the DeathLink latch only for the same seed."""

    async def connect(self, client: CVAOSClient, auth: bytes) -> None:
        ctx = SimpleNamespace(bizhawk_ctx=None, auth=None)
        with mock.patch.object(bizhawk, "read", mock.AsyncMock(return_value=[auth])):
            await client.set_auth(ctx)

    async def test_a_reconnect_to_the_same_seed_keeps_the_latch(self) -> None:
        client = CVAOSClient.__new__(CVAOSClient)
        await self.connect(client, bytes(16))
        client.currently_dead = True
        await self.connect(client, bytes(16))
        self.assertIs(client.currently_dead, True)

    async def test_a_different_seed_leaves_the_latch_unknown(self) -> None:
        client = CVAOSClient.__new__(CVAOSClient)
        await self.connect(client, bytes(16))
        client.currently_dead = True
        await self.connect(client, bytes(range(16)))
        self.assertIsNone(client.currently_dead)


if __name__ == "__main__":
    unittest.main()
