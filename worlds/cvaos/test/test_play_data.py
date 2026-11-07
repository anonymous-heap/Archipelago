"""The client's local play-data log (play_data.py, client_logic._record_play_data).

Each session's first lines state every recorded value, and after that a line is written only when
a value changes. These tests drive the shared logic over the dict-backed FakeBackend and write the
log into a temporary folder.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from .. import play_data
from ..client_logic import CVAOSClientLogic
from ..locations import flag_offset_to_location_id, location_id_to_name
from ..ram import AoSRAM, addresses as addr
from ..ram.addresses import EWRAM
from .test_ram_map import FakeBackend

ONE_HOUR = 60 * 60 * 60


def offset(entry) -> int:
    return entry.request().offset


class PlayDataTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.ewram = bytearray(0x40000)
        self.backend = FakeBackend({EWRAM: self.ewram})
        self.ram = AoSRAM(self.backend)
        self.client = CVAOSClientLogic()
        self.client._reset_session_state()
        self.ctx = SimpleNamespace(server_seed_name="4242", slot=1, player_names={1: "Soma"},
                                   slot_data={"goal": 1, "rom_given_locations": [1, 2]},
                                   server_locations=set(), checked_locations=set(),
                                   send_msgs=mock.AsyncMock())
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = mock.patch.object(play_data, "folder", return_value=folder.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = os.path.join(folder.name, "AP_4242_P1.jsonl")
        self.set_play_frames(ONE_HOUR)

    def set_play_frames(self, frames: int) -> None:
        start = offset(addr.PLAY_TIMER)
        self.ewram[start:start + 4] = frames.to_bytes(4, "little")

    async def record(self) -> list[dict]:
        """Record once, as if the interval had passed, and return the lines it wrote."""
        before = len(self.lines())
        self.client.last_play_data_time = -math.inf
        await self.client._record_play_data(self.ctx, self.ram)
        return self.lines()[before:]

    def lines(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as file:
            return [json.loads(line) for line in file]

    async def test_a_session_states_everything_then_only_changes(self) -> None:
        self.ewram[offset(addr.EQUIPPED_WEAPON)] = 2                      # Combat Knife
        self.ewram[offset(addr.EQUIPPED_ARMOR)] = 1                       # Cloth Tunic
        self.ewram[offset(addr.INVENTORY["red_soul"].entry)] = 0x10       # Bat, soul 1
        self.ewram[offset(addr.INVENTORY["blue_soul"].second)] = 0x01     # Flying Armor, 2nd list
        self.ewram[offset(addr.DROP_1_SEEN)] = 0x02                       # Zombie, enemy 1
        lines = await self.record()
        self.assertEqual(len(self.backend.read_batches), 1, "the values come in one read")
        self.assertEqual([line["event"] for line in lines],
                         ["session", "souls", "gear", "drops_seen"])
        self.assertEqual({line["play_frames"] for line in lines}, {ONE_HOUR})
        self.assertEqual((lines[0]["seed"], lines[0]["slot"], lines[0]["player"]),
                         ("4242", 1, "Soma"))
        self.assertEqual(lines[0]["options"], {"goal": 1}, "only single values are copied")
        self.assertEqual((lines[1]["owned"], lines[1]["gained"], lines[1]["lost"]),
                         (2, ["Bat", "Flying Armor"], []))
        self.assertEqual((lines[2]["weapon"], lines[2]["armor"]), ("Combat Knife", "Cloth Tunic"))
        self.assertEqual((lines[3]["slot_1"], lines[3]["slot_2"]), (["Zombie"], []))

        self.assertEqual(await self.record(), [], "nothing changed")
        self.ewram[offset(addr.INVENTORY["red_soul"].entry)] = 0x11       # Winged Skeleton too
        souls = await self.record()
        self.assertEqual([(line["event"], line["owned"], line["gained"]) for line in souls],
                         [("souls", 3, ["Winged Skeleton"])])

        self.client._reset_session_state(same_seed=True)
        self.assertEqual([line["event"] for line in await self.record()],
                         ["session", "souls", "gear", "drops_seen"])

    async def test_each_check_is_logged_once(self) -> None:
        flag_offset, location = min(flag_offset_to_location_id.items())
        self.ewram[offset(addr.PICKUP_FLAGS) + (flag_offset >> 3)] |= 1 << (flag_offset & 7)
        self.ctx.server_locations = {location}
        await self.client._send_location_checks(self.ctx, self.ram)
        checks = [line for line in await self.record() if line["event"] == "checks"]
        self.assertEqual([line["locations"] for line in checks], [[location_id_to_name[location]]])

        # A new session sends every check again, and the server already has this one.
        self.client._reset_session_state(same_seed=True)
        self.ctx.checked_locations = {location}
        await self.client._send_location_checks(self.ctx, self.ram)
        self.assertEqual([line for line in await self.record() if line["event"] == "checks"], [])

    async def test_a_time_line_marks_a_quiet_minute_and_an_older_save(self) -> None:
        await self.record()
        self.set_play_frames(ONE_HOUR + 60 * 60 - 1)
        self.assertEqual(await self.record(), [])
        self.set_play_frames(ONE_HOUR + 60 * 60)
        self.assertEqual([line["event"] for line in await self.record()], ["time"])
        self.set_play_frames(ONE_HOUR)
        self.assertEqual([line["event"] for line in await self.record()], ["time"])

    async def test_a_log_that_cannot_be_written_warns_once_and_never_raises(self) -> None:
        open(self.path, "w").close()
        with mock.patch.object(play_data, "folder", return_value=self.path):  # a file, not a folder
            with self.assertLogs("Client", level="WARNING") as logs:
                await self.record()
                self.set_play_frames(ONE_HOUR * 2)
                await self.record()
        self.assertEqual(len(logs.records), 1)


if __name__ == "__main__":
    unittest.main()
