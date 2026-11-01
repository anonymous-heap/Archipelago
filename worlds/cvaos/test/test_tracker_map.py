"""
Universal Tracker's map tab: the map data in tracker/ matches this world, and loads the way UT
loads it. The client also reports Soma's position for the map.

    python -m pytest worlds/cvaos/test/test_tracker_map.py -v
"""
from __future__ import annotations

import json
import os
import pkgutil
import unittest
from collections import Counter
from types import SimpleNamespace
from unittest import mock

from .. import CVAOSWorld, client_logic, tracker_map, tracker_png
from ..client_logic import CVAOSClientLogic
from ..data import map_info
from ..data.room_info import by_identifier as rooms_by_identifier
from ..locations import location_name_to_id
from ..tracker_map import BRIGHT


def ut_bytes(name: str) -> bytes:
    """Read a file in tracker/ as UT does: through pkgutil, relative to the World class's module."""
    path = f"/{CVAOSWorld.tracker_world['map_page_folder']}/{name}"
    return pkgutil.get_data(CVAOSWorld.__module__, path)


def ut_load(name: str):
    """Read a JSON file in tracker/ as UT does, as strict JSON."""
    return json.loads(ut_bytes(name).decode("utf-8-sig"))


def table_edges():
    """Yield every edge in the map's edge table, as the two cells it lies between and its kind."""
    for y in range(map_info.ROWS):
        for x in range(map_info.COLUMNS):
            yield ((x, y - 1), (x, y)), map_info.top_edge(x, y)
            yield ((x - 1, y), (x, y)), map_info.left_edge(x, y)


def plain_room(x: int, y: int) -> bool:
    """Whether a map cell holds a room that is neither a save room nor a warp room."""
    value = map_info.cell(x, y)
    return value != map_info.EMPTY and not value & (map_info.SAVE_ROOM | map_info.WARP_ROOM)


def find_rom() -> str | None:
    name = "Castlevania - Aria of Sorrow (USA)/Castlevania - Aria of Sorrow (USA).gba"
    candidates = [os.environ.get("CVAOS_USA_ROM", ""), "/mnt/c/Users/belmo/Downloads/" + name,
                  os.path.join(os.path.expanduser("~"), "Downloads", name)]
    return next((path for path in candidates if path and os.path.exists(path)), None)


class TrackerMapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.maps = ut_load(tracker_map.MAPS_FILE)
        self.locations = ut_load(tracker_map.LOCATIONS_FILE)
        self.index = {m.name: index for index, m in enumerate(tracker_map.MAPS)}

    def test_the_shipped_files_match_the_world_data(self):
        for name, data in tracker_map.files().items():
            shipped = ut_bytes(name)
            with self.subTest(file=name):
                if name.endswith(".png"):
                    # Compare pixels, not bytes, because another zlib may compress the same
                    # pixels differently.
                    self.assertEqual(tracker_png.decode(shipped), tracker_png.decode(data))
                else:
                    self.assertEqual(json.loads(shipped), json.loads(data),
                                     "rewrite them with: python -m worlds.cvaos.tracker_map")

    def test_every_location_is_a_section_once(self):
        # UT matches a section to a location by its name alone.
        names = [section["name"] for node in self.locations for section in node["sections"]]
        self.assertEqual(sorted(names), sorted(location_name_to_id))

    def test_every_location_is_on_its_overview_and_its_area(self):
        for node in self.locations:
            area = node["name"][0]
            expected = (["Chaotic Realm"] if area == tracker_map.REALM_AREA
                        else ["Castle", tracker_map.AREA_NAMES[area]])
            with self.subTest(location=node["sections"][0]["name"]):
                self.assertEqual([pin["map"] for pin in node["map_locations"]], expected)

    def test_every_pin_lands_on_a_drawn_room(self):
        images = {m["name"]: tracker_png.decode(ut_bytes(m["img"])) for m in self.maps}
        background = bytes(tracker_map.BACKGROUND)
        for node in self.locations:
            for pin in node["map_locations"]:
                width, height, pixels = images[pin["map"]]
                with self.subTest(location=node["sections"][0]["name"], map=pin["map"]):
                    self.assertTrue(0 <= pin["x"] < width and 0 <= pin["y"] < height)
                    at = (pin["y"] * width + pin["x"]) * 3
                    self.assertNotEqual(pixels[at:at + 3], background)

    def test_no_two_pins_share_a_spot(self):
        # UT keys a map's pins by their position, so a second pin there would replace the first.
        spots = Counter((pin["map"], pin["x"], pin["y"])
                        for node in self.locations for pin in node["map_locations"])
        self.assertEqual([spot for spot, count in spots.items() if count > 1], [])

    def test_a_pin_lands_where_its_pickup_is(self):
        # Entrance Parapet's pickup is 384 by 184 pixels into room 000, which starts at map cell
        # (0, 19).
        node = next(node for node in self.locations
                    if node["sections"][0]["name"] == "Entrance Parapet [000 Lucky Charm]")
        castle = next(pin for pin in node["map_locations"] if pin["map"] == "Castle")
        origin_x, origin_y = tracker_map.MAPS[0].origin
        self.assertEqual((castle["x"], castle["y"]), (origin_x + round(24 * (0 + 384 / 256)),
                                                      origin_y + round(24 * (19 + 184 / 256))))

    def test_soma_is_marked_at_the_centre_of_his_map_cell(self):
        # A point 384 by 184 pixels into room 000, which starts at cell (0, 19), lies in cell
        # (1, 19).
        castle, realm = 0, 1
        origin_x, origin_y = tracker_map.MAPS[castle].origin
        icon = tracker_map.position_icon(castle, ["000", 384, 184])
        self.assertEqual(icon, (origin_x + 36, origin_y + 468, tracker_map.MARKER_FILE))
        self.assertEqual(tracker_map.position_icon(castle, ["000", 300, 10]), icon, "same cell")
        self.assertEqual(tracker_map.position_icon(castle, ["000", 512, 184])[0], icon[0] + 24)
        self.assertIsNotNone(tracker_map.position_icon(self.index["Castle Corridor"],
                                                       ["000", 384, 184]))
        self.assertIsNone(tracker_map.position_icon(self.index["Chapel"], ["000", 384, 184]))
        self.assertIsNone(tracker_map.position_icon(realm, ["000", 384, 184]))
        self.assertIsNotNone(tracker_map.position_icon(realm, ["B14", 100, 100]))

    def test_soma_has_no_marker_until_ut_can_place_him(self):
        # UT passes "" or None as the position before the client first writes it, and None as
        # the map before one loads. Any exception here would disconnect UT.
        beyond = len(tracker_map.MAPS)
        for map_id, value in ((0, ""), (0, None), (None, ["000", 0, 0]), (beyond, ["000", 0, 0]),
                              (-1, ["000", 0, 0]), (0, ["ZZZ", 0, 0]), (0, ["000", "x", 0]),
                              (0, {"room": "000"}), (0, ["000", 0])):
            with self.subTest(map_id=map_id, value=value):
                self.assertIsNone(tracker_map.position_icon(map_id, value))

    def test_the_map_follows_soma_to_his_area(self):
        self.assertEqual(tracker_map.map_page_index(["000", 0, 0]), self.index["Castle Corridor"])
        self.assertEqual(tracker_map.map_page_index(["721", 0, 0]),
                         self.index["Underground Reservoir"])
        self.assertEqual(tracker_map.map_page_index(["B14", 0, 0]), self.index["Chaotic Realm"])
        for value in (["ZZZ", 0, 0], ["000"]):
            with self.subTest(value=value):
                self.assertEqual(tracker_map.map_page_index(value), -1, "keep the current map")

    def test_with_no_position_yet_the_whole_castle_shows(self):
        # UT shows no map until something selects one, and before the game starts there is no
        # position to select one.
        for value in ("", None):
            with self.subTest(value=value):
                self.assertEqual(tracker_map.map_page_index(value), self.index["Castle"])

    def test_the_dropdown_lists_every_map_once(self):
        listed = [node for node in tracker_map.MAP_GROUPS if isinstance(node, str)]
        listed += [name for node in tracker_map.MAP_GROUPS if not isinstance(node, str)
                   for name in node[1]]
        self.assertEqual(sorted(listed), sorted(m["name"] for m in self.maps))

    def test_the_grid_holds_every_room_where_room_info_puts_it(self):
        on_grid = {map_info.room_at(x, y) for y in range(map_info.ROWS)
                   for x in range(map_info.COLUMNS)} - {None}
        self.assertEqual(on_grid, set(rooms_by_identifier))
        # Room 901's first cell is drawn as part of room 900, as in the game.
        moved = [room for room, info in rooms_by_identifier.items()
                 if map_info.room_at(int(info.x), int(info.y)) != room]
        self.assertEqual(moved, ["901"])

    def test_the_edge_table_closes_off_every_room(self):
        for (a, b), kind in table_edges():
            rooms = (map_info.room_at(*a), map_info.room_at(*b))
            with self.subTest(cells=(a, b), rooms=rooms):
                if rooms[0] == rooms[1]:
                    self.assertEqual(kind, map_info.OPEN, "inside a room, or where there is none")
                else:
                    self.assertNotEqual(kind, map_info.OPEN)

    def test_every_door_between_two_rooms_is_an_entrance(self):
        from ..data.entrance_info import rows as entrances
        connected = {frozenset((door.room_identifier, door.dest_room_identifier))
                     for door in entrances}
        doors = [frozenset(map_info.room_at(*cell) for cell in cells)
                 for cells, kind in table_edges() if kind in (map_info.DOOR, map_info.HIDDEN_DOOR)]
        # A door with no room on one side leads to a room the map draws elsewhere.
        between_rooms = [rooms for rooms in doors if None not in rooms]
        self.assertGreater(len(between_rooms), 300)
        for rooms in between_rooms:
            with self.subTest(rooms=sorted(rooms)):
                self.assertIn(rooms, connected)

    def test_a_wall_two_rooms_share_is_drawn_once_on_the_line_between_them(self):
        castle = tracker_map.MAPS[0]
        width, _, pixels = tracker_png.decode(ut_bytes(castle.img))
        origin_x, origin_y = castle.origin
        half = tracker_map.WALL_PIXELS // 2
        for kind, colour in ((map_info.WALL, BRIGHT.wall), (map_info.DOOR, BRIGHT.door)):
            # Find two plain cells of different rooms, side by side, with this kind of edge
            # between them.
            x, y = next((x, y) for y in range(map_info.ROWS) for x in range(map_info.COLUMNS)
                        if map_info.left_edge(x, y) == kind
                        and plain_room(x - 1, y) and plain_room(x, y)
                        and map_info.room_at(x - 1, y) != map_info.room_at(x, y))
            line_x = origin_x + x * tracker_map.CELL_PIXELS
            row = origin_y + y * tracker_map.CELL_PIXELS + tracker_map.CELL_PIXELS // 2
            across = [bytes(pixels[(row * width + px) * 3:(row * width + px) * 3 + 3])
                      for px in range(line_x - half - 1, line_x + half + 1)]
            with self.subTest(kind=kind, cell=(x, y)):
                self.assertEqual(across, [bytes(BRIGHT.room)]
                                 + [bytes(colour)] * tracker_map.WALL_PIXELS + [bytes(BRIGHT.room)])

    def test_the_map_data_matches_the_rom(self):
        rom_path = find_rom()
        if rom_path is None:
            self.skipTest("USA ROM not available")
        with open(rom_path, "rb") as f:
            rom = f.read()
        self.assertEqual(map_info.extract(rom), map_info.grid)
        self.assertEqual(map_info.extract_edges(rom), map_info.edges)
        self.assertEqual(map_info.extract_hidden_doors(rom), map_info.hidden_doors)
        self.assertEqual(map_info.extract_castle_map_skips(rom), map_info.CASTLE_MAP_SKIPS)

    def test_the_hidden_door_list_holds_every_hidden_door_edge(self):
        on_edges = set()
        for (a, b), kind in table_edges():
            if kind == map_info.HIDDEN_DOOR:
                side = "top" if a[0] == b[0] else "left"
                on_edges.add(map_info.HiddenDoor(side, *b))
        self.assertEqual(set(map_info.hidden_doors), on_edges)
        self.assertEqual(len(map_info.hidden_doors), len(on_edges), "each door is listed once")


class FakeRam:
    def __init__(self) -> None:
        self.position = (0, 0, 384, 184)

    async def get_player_room_position(self):
        return self.position


class PositionReportTest(unittest.IsolatedAsyncioTestCase):
    """The client keeps Soma's position in data storage for UT's map tab and the tracker."""

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

    def sent_values(self) -> list[tuple[str, list]]:
        return [(msg["key"], msg["operations"][0]["value"]) for msg in self.sent]

    async def test_the_first_position_goes_to_both_keys(self):
        await self.client._report_position(self.ctx, self.ram)
        self.assertEqual(self.sent, [{
            "cmd": "Set", "key": key, "default": None, "want_reply": False,
            "operations": [{"operation": "replace", "value": ["000", 384, 184]}]}
            for key in ("cvaos_position_0_3", "cvaos_map_cell_0_3")])
        self.assertEqual(tracker_map.POSITION_KEY.format(team=0, player=3), "cvaos_position_0_3")
        self.assertEqual(tracker_map.MAP_CELL_KEY.format(team=0, player=3), "cvaos_map_cell_0_3")

    async def test_ut_reads_the_map_cell_key(self):
        # UT reloads its map whenever its key changes, so it must not read the moving position.
        self.assertEqual(CVAOSWorld.tracker_world["map_page_setting_key"], tracker_map.MAP_CELL_KEY)
        self.assertEqual(CVAOSWorld.tracker_world["location_setting_key"], tracker_map.MAP_CELL_KEY)

    async def test_a_move_within_the_cell_updates_only_the_position(self):
        await self.client._report_position(self.ctx, self.ram)
        self.sent.clear()
        self.now += 5
        self.ram.position = (0, 0, 500, 250)
        await self.client._report_position(self.ctx, self.ram)
        self.assertEqual(self.sent_values(), [("cvaos_position_0_3", ["000", 500, 250])])
        self.now += client_logic.POSITION_INTERVAL
        self.ram.position = (0, 0, 512, 250)
        await self.client._report_position(self.ctx, self.ram)
        self.assertEqual(self.sent_values()[1:], [("cvaos_position_0_3", ["000", 512, 250]),
                                                  ("cvaos_map_cell_0_3", ["000", 512, 250])])

    async def test_standing_still_sends_nothing(self):
        await self.client._report_position(self.ctx, self.ram)
        self.now += 5
        await self.client._report_position(self.ctx, self.ram)
        self.assertEqual(len(self.sent), 2)

    async def test_a_move_is_sent_at_most_once_per_interval(self):
        await self.client._report_position(self.ctx, self.ram)
        self.ram.position = (0, 2, 10, 10)
        await self.client._report_position(self.ctx, self.ram)
        self.assertEqual(len(self.sent), 2, "too soon after the last message")
        self.now += client_logic.POSITION_INTERVAL
        await self.client._report_position(self.ctx, self.ram)
        self.assertEqual(self.sent_values()[2:], [("cvaos_position_0_3", ["002", 10, 10]),
                                                  ("cvaos_map_cell_0_3", ["002", 10, 10])])

    async def test_a_new_session_sends_the_position_again(self):
        await self.client._report_position(self.ctx, self.ram)
        self.client._reset_session_state()
        self.now += client_logic.POSITION_INTERVAL
        await self.client._report_position(self.ctx, self.ram)
        self.assertEqual(len(self.sent), 4)


if __name__ == "__main__":
    unittest.main()
