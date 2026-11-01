"""
The explored map: how the client sends it, how the tracker reads it back, and which cells each
Castle Map reveals.

    python -m pytest worlds/cvaos/test/test_exploration.py -v
"""
from __future__ import annotations

import struct
import unittest
from types import SimpleNamespace
from unittest import mock

from .. import client_logic, exploration
from ..client_logic import CVAOSClientLogic
from ..data import map_info
from ..exploration import Exploration


def ram_cells(visited: set[tuple[int, int]], remembered: set[tuple[int, int]]) -> bytes:
    """Return the explored-map RAM for these cells, laid out as the game keeps it."""
    words = [[0, 0] for _ in range(2 * exploration.ROWS)]
    for which, cells in ((0, visited), (1, remembered)):
        for x, y in cells:
            words[(x // 32) * exploration.ROWS + y][which] |= 1 << (x % 32)
    return b"".join(struct.pack("<II", *pair) for pair in words)


class DecodeTest(unittest.TestCase):
    def test_cells_come_back_from_the_game_layout(self):
        visited = {(0, 19), (1, 19), (33, 0), (63, 39)}
        remembered = visited | {(2, 20)}
        decoded = exploration.decode(exploration.value(ram_cells(visited, remembered), 0))
        self.assertEqual((decoded.visited, decoded.remembered), (visited, remembered))

    def test_only_the_hidden_door_flags_are_kept(self):
        stored = exploration.value(bytes(exploration.CELLS_SIZE), 0xFFFFFFFF)
        self.assertEqual(stored["hidden_doors"], (1 << len(map_info.hidden_doors)) - 1)
        self.assertEqual(exploration.decode(stored).hidden_doors,
                         frozenset(range(len(map_info.hidden_doors))))

    def test_the_castle_maps_held_come_back(self):
        stored = exploration.value(bytes(exploration.CELLS_SIZE), 0, bytes([1, 0, 2]))
        self.assertEqual(exploration.decode(stored).castle_maps, {1, 3})

    def test_a_value_from_before_castle_maps_holds_none(self):
        stored = exploration.value(bytes(exploration.CELLS_SIZE), 0)
        del stored["castle_maps"]
        self.assertEqual(exploration.decode(stored).castle_maps, frozenset())

    def test_encode_is_the_inverse_of_decode(self):
        explored = Exploration(visited=frozenset({(5, 13), (40, 24)}),
                               remembered=frozenset({(5, 13), (40, 24), (6, 13)}),
                               hidden_doors=frozenset({0, 12}), castle_maps=frozenset({2}))
        self.assertEqual(exploration.decode(exploration.encode(explored)), explored)

    def test_anything_else_decodes_to_none(self):
        for stored in (None, "", {}, {"cells": "zz", "hidden_doors": 0},
                       {"cells": "00", "hidden_doors": 0}, {"cells": "", "hidden_doors": "x"}):
            with self.subTest(stored=stored):
                self.assertIsNone(exploration.decode(stored))


def area_cells(*areas: int) -> set[tuple[int, int]]:
    return {(x, y) for y in range(map_info.ROWS) for x in range(map_info.COLUMNS)
            if map_info.cell(x, y) != map_info.EMPTY and (map_info.cell(x, y) >> 6) & 0xF in areas}


class CastleMapTest(unittest.TestCase):
    def test_each_map_shows_its_areas_but_not_their_secret_rooms(self):
        secret = set(map_info.CASTLE_MAP_SKIPS)
        for number, areas in ((1, (0, 1)), (2, (3,)), (3, (5,))):
            with self.subTest(castle_map=number):
                shown = exploration.revealed_by_castle_maps(frozenset({number}))
                self.assertEqual(shown, area_cells(*areas) - secret)

    def test_the_secret_rooms_are_in_the_areas_the_maps_show(self):
        self.assertLessEqual(set(map_info.CASTLE_MAP_SKIPS), area_cells(0, 1, 3, 5))

    def test_no_map_shows_nothing(self):
        self.assertEqual(exploration.revealed_by_castle_maps(frozenset()), frozenset())


class FakeRam:
    def __init__(self) -> None:
        self.cells = ram_cells({(0, 19)}, {(0, 19)})
        self.event_flags = 0
        self.castle_maps = bytes(3)
        self.reads = 0

    async def read_exploration(self):
        self.reads += 1
        return self.cells, self.event_flags, self.castle_maps


class ExplorationReportTest(unittest.IsolatedAsyncioTestCase):
    """The client keeps the explored map in data storage for the tracker."""

    async def asyncSetUp(self) -> None:
        self.client = CVAOSClientLogic()
        self.client._reset_session_state()
        self.sent: list[dict] = []

        async def send_msgs(messages):
            self.sent.extend(messages)

        self.ctx = SimpleNamespace(team=0, slot=3, send_msgs=send_msgs)
        self.ram = FakeRam()
        self.now = 1000.0
        patcher = mock.patch.object(client_logic.time, "monotonic", side_effect=lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def report(self) -> None:
        await self.client._report_exploration(self.ctx, self.ram)

    async def test_the_map_goes_to_the_tracker_key(self):
        await self.report()
        self.assertEqual(len(self.sent), 1)
        message = self.sent[0]
        self.assertEqual((message["cmd"], message["key"]), ("Set", "cvaos_explored_0_3"))
        stored = message["operations"][0]["value"]
        self.assertEqual(exploration.decode(stored).visited, {(0, 19)})

    async def test_an_unchanged_map_is_not_sent_again(self):
        await self.report()
        self.now += client_logic.EXPLORATION_INTERVAL
        await self.report()
        self.assertEqual((self.ram.reads, len(self.sent)), (2, 1))

    async def test_the_map_is_read_only_once_per_interval(self):
        await self.report()
        self.ram.cells = ram_cells({(0, 19), (1, 19)}, {(0, 19), (1, 19)})
        await self.report()
        self.assertEqual((self.ram.reads, len(self.sent)), (1, 1), "read too soon")
        self.now += client_logic.EXPLORATION_INTERVAL
        await self.report()
        self.assertEqual(exploration.decode(self.sent[-1]["operations"][0]["value"]).visited,
                         {(0, 19), (1, 19)})

    async def test_a_hidden_door_gone_through_is_sent(self):
        await self.report()
        self.now += client_logic.EXPLORATION_INTERVAL
        self.ram.event_flags = 1 << 3
        await self.report()
        self.assertEqual(exploration.decode(self.sent[-1]["operations"][0]["value"]).hidden_doors,
                         {3})

    async def test_a_castle_map_picked_up_is_sent(self):
        await self.report()
        self.now += client_logic.EXPLORATION_INTERVAL
        self.ram.castle_maps = bytes([0, 0, 1])
        await self.report()
        self.assertEqual(exploration.decode(self.sent[-1]["operations"][0]["value"]).castle_maps,
                         {3})

    async def test_a_new_session_sends_the_map_again(self):
        await self.report()
        self.client._reset_session_state()
        await self.report()
        self.assertEqual(len(self.sent), 2)


if __name__ == "__main__":
    unittest.main()
