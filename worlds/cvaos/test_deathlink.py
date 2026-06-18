"""
Tests for the cvaos DeathLink relay (client._relay_deathlink).

The relay keys off the engine's death STATE (MENU_STATE==DEATH during the in-game death fade, then
the GAME_OVER screen) rather than HP==0, because every death clamps HP to 0 and flips MENU_STATE to
the death sub-state on the same frame -- a state that persists for many frames, unlike the ~1-frame
(INGAME, NORMAL, hp==0) window the old check relied on and almost always missed.

These tests drive _relay_deathlink through the relevant transitions with a fake context and a fake
RAM accessor, so they need no emulator. Run from the Archipelago root:
    python -m pytest worlds/cvaos/test_deathlink.py -v
"""
from __future__ import annotations

import unittest

from .client import CVAOSClient
from .ram import addresses as addr

INGAME = int(addr.GameState.INGAME)
GAME_OVER = int(addr.GameState.GAME_OVER)
NORMAL = addr.MENU_STATE_NORMAL
DEATH = addr.MENU_STATE_DEATH


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
    # DeathLink state set_auth would normally initialize.
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
        await self.relay(INGAME, DEATH, hp=0)       # mid-fade: unsafe to poke HP
        self.assertEqual(self.ram.kill_calls, 0)
        self.assertEqual(self.client.death_causes, ["Someone else died"], "cause stays queued")

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


if __name__ == "__main__":
    unittest.main()
