"""
Universal Tracker's map tab: the map data in tracker/ matches this world, and loads the way UT
loads it.

    python -m pytest worlds/cvaos/test/test_tracker_map.py -v
"""
from __future__ import annotations

import json
import pkgutil
import unittest
from collections import Counter
from types import SimpleNamespace
from unittest import mock

from .. import CVAOSSettings, CVAOSWorld, client_logic, tracker_map
from ..client_logic import CVAOSClientLogic
from ..locations import location_name_to_id


def ut_load(name: str):
    """Read a file in tracker/ as UT does: through pkgutil, relative to the World class's module,
    then as strict JSON."""
    path = f"/{CVAOSWorld.tracker_world['map_page_folder']}/{name}"
    return json.loads(pkgutil.get_data(CVAOSWorld.__module__, path).decode("utf-8-sig"))


class TrackerMapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.maps = ut_load(tracker_map.MAPS_FILE)
        self.locations = ut_load(tracker_map.LOCATIONS_FILE)

    def test_the_shipped_files_match_the_world_data(self):
        maps, locations = tracker_map.build()
        self.assertEqual(self.maps, maps)
        self.assertEqual(self.locations, locations,
                         "rewrite them with: python -m worlds.cvaos.tracker_map")

    def test_every_location_is_a_section_once(self):
        # UT matches a section to a location by its name alone.
        names = [section["name"] for node in self.locations for section in node["sections"]]
        self.assertEqual(sorted(names), sorted(location_name_to_id))

    def test_every_pin_is_on_a_known_map_and_inside_its_image(self):
        sizes = {m.name: m.size for m in tracker_map.MAPS}
        self.assertEqual([m["name"] for m in self.maps], list(sizes))
        for node in self.locations:
            for pin in node["map_locations"]:
                with self.subTest(location=node["sections"][0]["name"], map=pin["map"]):
                    width, height = sizes[pin["map"]]
                    self.assertTrue(0 <= pin["x"] < width and 0 <= pin["y"] < height)

    def test_no_two_pins_share_a_spot(self):
        # UT keys a map's pins by their position, so a second pin there would replace the first.
        spots = Counter((pin["map"], pin["x"], pin["y"])
                        for node in self.locations for pin in node["map_locations"])
        self.assertEqual([spot for spot, count in spots.items() if count > 1], [])

    def test_a_pin_lands_where_its_pickup_is(self):
        # Entrance Parapet's pickup is 384 by 184 pixels into room 000, at map cell (0, 19).
        node = next(node for node in self.locations
                    if node["sections"][0]["name"] == "Entrance Parapet [000 Lucky Charm]")
        castle = next(pin for pin in node["map_locations"] if pin["map"] == "Dracula's Castle")
        self.assertEqual((castle["x"], castle["y"]),
                         (86 + round(24 * (0 + 384 / 256)), 49 + round(24 * (19 + 184 / 256))))

    def test_soma_is_drawn_at_the_centre_of_his_map_cell(self):
        # 384 by 184 pixels into room 000, whose cell is (0, 19), is in cell (1, 19).
        castle, realm, combined = range(3)
        icon = tracker_map.position_icon(castle, ["000", 384, 184])
        self.assertEqual(icon, (86 + round(24 * 1.5), 49 + round(24 * 19.5),
                                tracker_map.POSITION_ICON))
        self.assertEqual(tracker_map.position_icon(castle, ["000", 300, 10]), icon, "same cell")
        self.assertEqual(tracker_map.position_icon(castle, ["000", 512, 184])[0], icon[0] + 24)
        self.assertEqual(tracker_map.position_icon(combined, ["000", 384, 184]), icon)
        self.assertIsNone(tracker_map.position_icon(realm, ["000", 384, 184]),
                          "the Chaotic Realm map does not show the castle")
        self.assertIsNotNone(tracker_map.position_icon(realm, ["B14", 100, 100]))

    def test_soma_has_no_icon_until_ut_can_place_him(self):
        # UT passes "" or None before the client writes, and None before a map loads. Any
        # exception would disconnect UT.
        for map_id, value in ((0, ""), (0, None), (None, ["000", 0, 0]), (3, ["000", 0, 0]),
                              (-1, ["000", 0, 0]), (0, ["ZZZ", 0, 0]), (0, ["000", "x", 0]),
                              (0, {"room": "000"}), (0, ["000", 0])):
            with self.subTest(map_id=map_id, value=value):
                self.assertIsNone(tracker_map.position_icon(map_id, value))

    def test_the_pack_path_is_an_optional_setting(self):
        key = CVAOSWorld.tracker_world["external_pack_key"]
        self.assertIn(key, CVAOSSettings.__annotations__)
        self.assertFalse(CVAOSSettings.UTPoptrackerPath.required)



class FakeRam:
    def __init__(self) -> None:
        self.position = (0, 0, 384, 184)

    async def get_player_room_position(self):
        return self.position


class PositionReportTest(unittest.IsolatedAsyncioTestCase):
    """The client keeps Soma's position in data storage for the tracker's map."""

    def setUp(self) -> None:
        self.client = CVAOSClientLogic()
        self.client._reset_session_state()
        self.sent: list[dict] = []

        async def send_msgs(msgs):
            self.sent.extend(msgs)

        self.ctx = SimpleNamespace(team=0, slot=3, send_msgs=send_msgs)
        self.ram = FakeRam()
        self.now = 100.0
        clock = mock.patch.object(client_logic.time, "monotonic", lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    async def test_the_position_goes_to_the_key_ut_reads(self):
        await self.client._report_position(self.ctx, self.ram)
        self.assertEqual(self.sent, [{
            "cmd": "Set", "key": "cvaos_position_0_3", "default": None, "want_reply": False,
            "operations": [{"operation": "replace", "value": ["000", 384, 184]}]}])
        self.assertEqual(tracker_map.POSITION_KEY.format(team=0, player=3), "cvaos_position_0_3")

    async def test_a_move_within_the_cell_is_not_sent(self):
        await self.client._report_position(self.ctx, self.ram)
        self.now += 5
        self.ram.position = (0, 0, 500, 250)
        await self.client._report_position(self.ctx, self.ram)
        self.assertEqual(len(self.sent), 1)
        self.ram.position = (0, 0, 512, 250)
        await self.client._report_position(self.ctx, self.ram)
        self.assertEqual(self.sent[-1]["operations"][0]["value"], ["000", 512, 250])

    async def test_a_new_room_is_sent_but_not_twice_in_one_interval(self):
        await self.client._report_position(self.ctx, self.ram)
        self.ram.position = (0, 2, 10, 10)
        await self.client._report_position(self.ctx, self.ram)
        self.assertEqual(len(self.sent), 1, "too soon after the last message")
        self.now += client_logic.POSITION_INTERVAL
        await self.client._report_position(self.ctx, self.ram)
        self.assertEqual(self.sent[-1]["operations"][0]["value"], ["002", 10, 10])

    async def test_a_new_session_sends_the_position_again(self):
        await self.client._report_position(self.ctx, self.ram)
        self.client._reset_session_state()
        self.now += client_logic.POSITION_INTERVAL
        await self.client._report_position(self.ctx, self.ram)
        self.assertEqual(len(self.sent), 2)


if __name__ == "__main__":
    unittest.main()
