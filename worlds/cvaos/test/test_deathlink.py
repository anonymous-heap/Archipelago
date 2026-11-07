"""
Tests for the cvaos DeathLink relay (client._relay_deathlink).

The relay keys off the engine's death STATE (MENU_STATE==DEATH during the in-game death fade, then
the GAME_OVER screen) rather than HP==0, because every death clamps HP to 0 and flips MENU_STATE to
the death sub-state on the same frame -- a state that persists for many frames, unlike the ~1-frame
(INGAME, NORMAL, hp==0) window the old check relied on and almost always missed.

These tests drive _relay_deathlink through the relevant transitions with a fake context and a fake
RAM accessor, so they need no emulator. DeathLinkTickTest runs whole ticks instead, so the ROM hook
can run between two of a tick's reads. Run from the Archipelago root:
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
    """Fake AoSRAM: the run state, HP and the kill-request flag are whatever we set. request_kill
    sets the flag and does NOT touch HP; run_hook does what the real ROM hook does with the flag."""

    def __init__(self, hp: int = 100) -> None:
        self.game_state, self.menu_state = INGAME, NORMAL
        self.hp = hp
        self.max_hp = 320
        self.kill_calls = 0
        self.kill_request = False
        # When set, the ROM hook runs after this many more read requests (see _read).
        self.reads_before_hook: int | None = None

    def _read(self, value):
        # Each read request lands on its own frame. A scheduled hook runs after the request that
        # ends its count, so the next request sees what it did.
        if self.reads_before_hook is not None:
            self.reads_before_hook -= 1
            if self.reads_before_hook == 0:
                self.reads_before_hook = None
                self.run_hook()
        return value

    def run_hook(self) -> None:
        # One frame of the ROM hook. It runs a requested kill unless Soma is already dying, and
        # the death routine it starts enters the death fade without zeroing HP.
        if self.kill_request and self.menu_state == NORMAL:
            self.kill_request = False
            self.menu_state = DEATH

    async def get_run_state(self) -> tuple[int, int]:
        return self._read((self.game_state, self.menu_state))

    async def get_deathlink_state(self) -> tuple[int, int, int, bool]:
        return self._read((self.game_state, self.menu_state, self.hp, self.kill_request))

    async def get_max_hp(self) -> int:
        return self._read(self.max_hp)

    async def ensure_game_cleared(self) -> bool:
        return False

    async def request_kill(self) -> None:
        self.kill_calls += 1
        self.kill_request = True

    async def clear_kill_request(self) -> None:
        self.kill_request = False


def _new_client() -> CVAOSClient:
    # Bypass BizHawkClient.__init__ (registration / emulator wiring) and set just the per-session
    # DeathLink state. currently_dead starts False, as if a tick had already found Soma alive.
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
        self.ram.game_state, self.ram.menu_state = game_state, menu_state
        if hp is not None:
            self.ram.hp = hp
        await self.client._relay_deathlink(self.ctx, self.ram)

    async def test_enables_death_link_from_slot_data(self) -> None:
        self.assertNotIn("DeathLink", self.ctx.tags)
        await self.relay(INGAME, NORMAL)
        self.assertIn("DeathLink", self.ctx.tags)

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

    async def test_rearms_only_after_the_incoming_kill_is_consumed(self) -> None:
        # Right after the client requests an incoming kill, the hook has not run yet. The state
        # reads NORMAL and HP is untouched, but the kill-request flag is still set. Re-arming then
        # would let the impending death echo back out as Soma's own.
        self.client.currently_dead = True
        self.ram.kill_request = True
        await self.relay(INGAME, NORMAL, hp=100)
        self.assertTrue(self.client.currently_dead, "a pending kill must not count as a respawn")
        self.ram.kill_request = False                   # the hook ran: death, then respawn
        await self.relay(INGAME, NORMAL, hp=100)        # alive with nothing pending, so re-arm
        self.assertFalse(self.client.currently_dead)

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

    # A reconnect to the same seed: set_auth runs the reset again, which keeps the latch and any
    # pending death.
    def reconnect(self) -> None:
        self.client._reset_session_state(same_seed=True)

    # A first connect, or a different seed: the reset drops any pending death, and the latch is
    # unknown until a relay reads the game.
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

    async def test_a_received_death_is_applied_and_not_rebroadcast(self) -> None:
        # Applying an incoming death must not later echo back out as our own broadcast.
        await self.relay(INGAME, NORMAL)
        self.client.death_causes.append("Someone else died")
        await self.relay(INGAME, NORMAL, hp=100)    # apply incoming: kill requested
        self.assertEqual(self.ram.kill_calls, 1)
        self.assertTrue(self.client.currently_dead)
        self.assertEqual(self.client.death_causes, [], "the cause must be consumed")
        sent_before = len(self.ctx.sent_deaths)
        await self.relay(INGAME, DEATH, hp=0)       # the game now enters its death fade
        self.assertEqual(len(self.ctx.sent_deaths), sent_before,
                         "a received death must not be rebroadcast as our own")


class DeathLinkTickTest(unittest.IsolatedAsyncioTestCase):
    """Whole client ticks, so the ROM hook can run on a frame between two of a tick's reads."""

    @staticmethod
    def ticking_client() -> CVAOSClient:
        client = _new_client()
        # Other tests cover the save-block concerns, so here they are stubs that touch no RAM.
        for concern in ("_send_location_checks", "_report_position", "_report_exploration",
                        "_receive_items", "_grant_progressive_movement", "_report_goal",
                        "_record_play_data"):
            setattr(client, concern, mock.AsyncMock())
        client._save_from_another_seed = mock.AsyncMock(return_value=False)
        return client

    def setUp(self) -> None:
        self.client = self.ticking_client()
        self.ctx = FakeCtx(death_link=True)
        self.ram = FakeRam(hp=100)

    async def tick(self) -> None:
        await self.client._tick(self.ctx, self.ram)

    async def test_a_kill_run_between_two_reads_is_not_broadcast(self) -> None:
        # The new-soul effect holds the ROM hook back while MENU_STATE stays NORMAL, so a requested
        # kill can run partway through a tick. Whichever read it follows, the relay must not count
        # that kill as Soma's own death.
        for reads_before_hook in (1, 2):
            with self.subTest(reads_before_hook=reads_before_hook):
                client = self.ticking_client()
                ctx = FakeCtx(death_link=True)
                ram = FakeRam(hp=100)
                await client._tick(ctx, ram)
                client.death_causes.append("Someone else died")
                await client._tick(ctx, ram)         # the kill is requested
                ram.reads_before_hook = reads_before_hook
                await client._tick(ctx, ram)         # the hook runs the kill during this tick
                await client._tick(ctx, ram)         # Soma is in the death fade
                self.assertEqual(ctx.sent_deaths, [],
                                 "a received death must not be rebroadcast as our own")

    async def test_a_kill_request_left_set_by_a_death_is_cleared(self) -> None:
        await self.tick()
        self.client.death_causes.append("Someone else died")
        await self.tick()                            # the kill is requested
        # Soma takes a lethal hit on the frame the request lands. He is already dying when the hook
        # reads the flag, so the hook skips the kill and leaves the flag set.
        self.ram.menu_state, self.ram.hp = DEATH, 0
        self.ram.run_hook()
        await self.tick()                            # the client sees the death fade
        self.ram.game_state = GAME_OVER
        await self.tick()
        self.assertFalse(self.ram.kill_request, "Soma has died, so the request must be withdrawn")
        # He loads a save, which leaves the flag as it was.
        self.ram.game_state, self.ram.menu_state, self.ram.hp = INGAME, NORMAL, 100
        self.ram.run_hook()
        self.assertEqual(self.ram.menu_state, NORMAL,
                         "a kill skipped during a death must not run after Soma loads a save")


class SetAuthTest(unittest.IsolatedAsyncioTestCase):
    """set_auth keeps the DeathLink latch when the client reconnects to the same seed, and resets
    it to unknown for a different seed."""

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
