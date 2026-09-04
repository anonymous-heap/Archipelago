"""_tick touches the save-data block only once a file is really loaded (client_logic._tick).

GAME_STATE and MENU_STATE report normal gameplay before the save block is populated: seen live as
an all-zero inventory and souls-collected count with both bytes already in their in-gameplay
values. The pickup flags, the inventory and the received-counter all live in that block, so a
grant made then goes into memory the load is about to overwrite, and if the load wins the
counter write the item is handed out again next tick. Max HP is in the same block and is never 0
for a loaded file, so _tick requires it to be non-zero before reading or writing any of the three.

These tests drive _tick with a fake context and RAM, recording which save-block concerns ran.
"""
from __future__ import annotations

import unittest

from ..ram import addresses as addr
from .test_deathlink import FakeCtx, _new_client

INGAME = int(addr.GameState.INGAME)
TITLE = 0
NORMAL = addr.MENU_STATE_NORMAL
PAUSE = addr.MENU_STATE_PAUSE


class FakeRam:
    """Just what _tick reads directly. The save-block concerns are stubbed on the client."""

    def __init__(self, game_state: int, menu_state: int, max_hp: int) -> None:
        self.game_state, self.menu_state, self.max_hp = game_state, menu_state, max_hp
        self.max_hp_reads = 0

    async def get_run_state(self) -> tuple[int, int]:
        return self.game_state, self.menu_state

    async def get_max_hp(self) -> int:
        self.max_hp_reads += 1
        return self.max_hp

    async def get_current_hp(self) -> int:
        return self.max_hp

    async def get_kill_request(self) -> bool:
        return False

    async def ensure_game_cleared(self) -> bool:
        return False

    async def ensure_hard_mode(self) -> bool:
        return False


class TickSaveBlockGateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = _new_client()
        self.ctx = FakeCtx(death_link=False)
        self.ran: list[str] = []

        def recorder(name: str):
            async def run(ctx, ram) -> None:
                self.ran.append(name)
            return run

        self.client._send_location_checks = recorder("checks")
        self.client._receive_items = recorder("items")
        self.client._report_goal = recorder("goal")

    async def tick(self, game_state: int, menu_state: int, max_hp: int) -> FakeRam:
        ram = FakeRam(game_state, menu_state, max_hp)
        await self.client._tick(self.ctx, ram)
        return ram

    async def test_loaded_file_in_normal_play_runs_everything(self) -> None:
        await self.tick(INGAME, NORMAL, max_hp=250)
        self.assertEqual(self.ran, ["checks", "items", "goal"])

    async def test_normal_play_with_an_unloaded_save_block_runs_nothing(self) -> None:
        # The live-observed trap: in-gameplay bytes, zeroed save block.
        await self.tick(INGAME, NORMAL, max_hp=0)
        self.assertEqual(self.ran, [], "a grant here would land in memory the load overwrites")

    async def test_paused_runs_nothing_and_does_not_bother_reading_max_hp(self) -> None:
        ram = await self.tick(INGAME, PAUSE, max_hp=250)
        self.assertEqual(self.ran, [])
        self.assertEqual(ram.max_hp_reads, 0, "the cheap state test should short-circuit")

    async def test_title_screen_runs_nothing(self) -> None:
        await self.tick(TITLE, NORMAL, max_hp=0)
        self.assertEqual(self.ran, [])


if __name__ == "__main__":
    unittest.main()
