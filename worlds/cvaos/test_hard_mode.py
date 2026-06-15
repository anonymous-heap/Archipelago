"""
Tests for Hard Mode enforcement: the client forces the game-mode difficulty nibble to Hard so
the HARD_PICKUP entities actually spawn. See ram.accessors.ensure_hard_mode and client.game_watcher.

Run from the Archipelago root:
    python -m pytest worlds/cvaos/test_hard_mode.py -v
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from .client import CVAOSClient
from .ram import addresses as addr
from .ram.accessors import AoSRAM

INGAME = int(addr.GameState.INGAME)


class EnsureHardModeTest(unittest.IsolatedAsyncioTestCase):
    async def _run(self, current_byte: int):
        ram = AoSRAM.__new__(AoSRAM)  # no real BizHawk ctx needed; read_u8/write_u8 are mocked
        ram.read_u8 = AsyncMock(return_value=current_byte)
        ram.write_u8 = AsyncMock()
        changed = await ram.ensure_hard_mode()
        return changed, ram.write_u8

    async def test_soma_normal_becomes_soma_hard(self):
        changed, write = await self._run(0x01)  # Soma, normal
        self.assertTrue(changed)
        write.assert_awaited_once_with(addr.GAME_MODE, 0x11)  # Soma, hard

    async def test_already_hard_is_a_no_op(self):
        changed, write = await self._run(0x11)  # Soma, hard
        self.assertFalse(changed)
        write.assert_not_awaited()

    async def test_preserves_character_nibble(self):
        changed, write = await self._run(0x00)  # Julius, normal
        self.assertTrue(changed)
        write.assert_awaited_once_with(addr.GAME_MODE, 0x10)  # difficulty nibble forced, char kept

    async def test_forces_any_non_hard_difficulty_nibble(self):
        changed, write = await self._run(0x21)  # high nibble 2
        self.assertTrue(changed)
        write.assert_awaited_once_with(addr.GAME_MODE, 0x11)


class EnsureGameClearedTest(unittest.IsolatedAsyncioTestCase):
    async def _run(self, current_byte: int):
        ram = AoSRAM.__new__(AoSRAM)
        ram.read_u8 = AsyncMock(return_value=current_byte)
        ram.write_u8 = AsyncMock()
        changed = await ram.ensure_game_cleared()
        return changed, ram.write_u8

    async def test_not_cleared_becomes_cleared(self):
        changed, write = await self._run(0x00)
        self.assertTrue(changed)
        write.assert_awaited_once_with(addr.GAME_CLEARED_FLAGS, 0x03)

    async def test_already_cleared_is_a_no_op(self):
        changed, write = await self._run(0x03)
        self.assertFalse(changed)
        write.assert_not_awaited()

    async def test_sets_the_missing_bit(self):
        changed, write = await self._run(0x01)  # only bit 0 set
        self.assertTrue(changed)
        write.assert_awaited_once_with(addr.GAME_CLEARED_FLAGS, 0x03)

    async def test_preserves_unrelated_bits(self):
        changed, write = await self._run(0x04)
        self.assertTrue(changed)
        write.assert_awaited_once_with(addr.GAME_CLEARED_FLAGS, 0x07)


class GameWatcherRamForcingTest(unittest.IsolatedAsyncioTestCase):
    async def _watch(self, *, hard_mode, game_state, menu_state=addr.MENU_STATE_NORMAL):
        client = CVAOSClient.__new__(CVAOSClient)
        client._relay_deathlink = AsyncMock()
        client._send_location_checks = AsyncMock()
        client._receive_items = AsyncMock()
        client._report_goal = AsyncMock()

        fake_ram = AsyncMock()
        fake_ram.get_run_state = AsyncMock(return_value=(game_state, menu_state))

        ctx = SimpleNamespace(server=object(), slot=1, bizhawk_ctx=object(),
                              slot_data={"hard_mode": hard_mode, "death_link": 0})
        with patch("worlds.cvaos.client.AoSRAM", return_value=fake_ram):
            await client.game_watcher(ctx)
        return fake_ram

    async def test_hard_mode_enforced_when_hard_and_ingame(self):
        fr = await self._watch(hard_mode=1, game_state=INGAME)
        fr.ensure_hard_mode.assert_awaited()

    async def test_hard_mode_not_enforced_when_option_off(self):
        fr = await self._watch(hard_mode=0, game_state=INGAME)
        fr.ensure_hard_mode.assert_not_awaited()

    async def test_cleared_flag_set_for_all_seeds_in_game(self):
        # Even with Hard Mode off, every seed gets the cleared-data flag while in-game.
        fr = await self._watch(hard_mode=0, game_state=INGAME)
        fr.ensure_game_cleared.assert_awaited()

    async def test_nothing_forced_outside_ingame(self):
        fr = await self._watch(hard_mode=1, game_state=int(addr.GameState.TITLE), menu_state=0)
        fr.ensure_hard_mode.assert_not_awaited()
        fr.ensure_game_cleared.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
