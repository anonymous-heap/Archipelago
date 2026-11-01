"""
A failed request or a dropped connector during a tick does not end the BizHawk client's game
watcher, so the next tick tries again.

    python -m pytest worlds/cvaos/test/test_client_watcher.py -v
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import worlds._bizhawk as bizhawk

from .test_deathlink import _new_client


class GameWatcherTest(unittest.IsolatedAsyncioTestCase):
    async def run_tick_raising(self, error: Exception) -> None:
        client = _new_client()
        ctx = SimpleNamespace(server=object(), slot=1, bizhawk_ctx=None)
        with mock.patch.object(type(client), "_tick",
                               new=mock.AsyncMock(side_effect=error)) as tick:
            await client.game_watcher(ctx)
        tick.assert_awaited_once()

    async def test_a_failed_request_is_retried_next_tick(self) -> None:
        await self.run_tick_raising(bizhawk.RequestFailedError("timed out"))

    async def test_a_dropped_connector_is_retried_next_tick(self) -> None:
        await self.run_tick_raising(bizhawk.NotConnectedError("not connected"))


if __name__ == "__main__":
    unittest.main()
