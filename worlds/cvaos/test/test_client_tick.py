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

    async def get_deathlink_state(self) -> tuple[int, int, int, bool]:
        return self.game_state, self.menu_state, self.max_hp, False

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
        self.from_another_seed = False

        async def save_check(ctx, ram) -> bool:
            return self.from_another_seed

        self.client._save_from_another_seed = save_check  # tested in test_save_guard

        async def ignore(ctx, ram) -> None:
            pass

        self.client._report_position = ignore  # tested in test_tracker_map
        self.client._report_exploration = ignore  # tested in test_exploration
        self.client._receive_items = recorder("items")
        self.client._grant_progressive_movement = recorder("progressive")
        self.client._report_goal = recorder("goal")

    async def tick(self, game_state: int, menu_state: int, max_hp: int) -> FakeRam:
        ram = FakeRam(game_state, menu_state, max_hp)
        await self.client._tick(self.ctx, ram)
        return ram

    async def test_loaded_file_in_normal_play_runs_everything(self) -> None:
        await self.tick(INGAME, NORMAL, max_hp=250)
        self.assertEqual(self.ran, ["checks", "items", "progressive", "goal"])

    async def test_a_save_from_another_seed_sends_no_checks_and_no_goal(self) -> None:
        self.from_another_seed = True
        await self.tick(INGAME, NORMAL, max_hp=250)
        self.assertEqual(self.ran, ["items", "progressive"])

    async def test_normal_play_with_an_unloaded_save_block_runs_nothing(self) -> None:
        # The live-observed trap: in-gameplay bytes, zeroed save block.
        await self.tick(INGAME, NORMAL, max_hp=0)
        self.assertEqual(self.ran, [], "a grant here would land in memory the load overwrites")

    async def test_outside_normal_play_runs_nothing_and_does_not_read_max_hp(self) -> None:
        # Max HP is a loaded file's, so only the game-state check can stop the save-block concerns.
        for game_state, menu_state in ((TITLE, NORMAL), (INGAME, PAUSE)):
            with self.subTest(game_state=game_state, menu_state=menu_state):
                self.ran.clear()
                ram = await self.tick(game_state, menu_state, max_hp=250)
                self.assertEqual(self.ran, [])
                self.assertEqual(ram.max_hp_reads, 0, "the cheap state test should short-circuit")


if __name__ == "__main__":
    unittest.main()
