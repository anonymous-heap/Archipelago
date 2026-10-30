"""
The Aria of Sorrow tracker: its copy of UT's engine, its map, what it reports, and its web server.

The context is driven as the server would drive it, without a server: the tests set the slot's
details, then pass the packets the server would send.

    python -m pytest worlds/cvaos/test/test_tracker_app.py -v
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import pkgutil
import unittest
import urllib.error
import urllib.request

import websockets
from NetUtils import NetworkItem, NetworkSlot, SlotType
from worlds.LauncherComponents import components

from .. import CVAOSWorld, exploration
from ..exploration import Exploration
from ..locations import location_name_to_id
from ..tracker_app import parse_args
from ..tracker_app.context import TrackerContext
from ..tracker_app.mapdata import map_data
from ..tracker_app.server import TrackerServer
from ..tracker_app.ut import UPSTREAM
from .test_universal_tracker import generate

TERRACE = "Terrace [503 CASTLE MAP 3]"
LUCKY_CHARM = "Entrance Parapet [000 Lucky Charm]"


class UpstreamCopyTest(unittest.TestCase):
    def test_trackercore_is_ut_s_file_unchanged(self):
        # A Windows checkout may have CRLF line endings, so the hash is taken with LF.
        data = pkgutil.get_data("worlds.cvaos.tracker_app.ut", "TrackerCore.py")
        data = data.replace(b"\r\n", b"\n")
        self.assertEqual(hashlib.sha256(data).hexdigest(), UPSTREAM["sha256"],
                         "copy TrackerCore.py from UT again, or record the new version in UPSTREAM")

    def test_the_license_ships_with_it(self):
        self.assertIn(b"MIT License", pkgutil.get_data("worlds.cvaos.tracker_app.ut", "LICENSE"))


class MapDataTest(unittest.TestCase):
    def setUp(self) -> None:
        self.data = map_data()

    def test_every_location_has_a_pin(self):
        self.assertEqual(sorted(location["id"] for location in self.data["locations"]),
                         sorted(location_name_to_id.values()))

    def test_the_chaotic_realm_comes_last(self):
        self.assertEqual([area["realm"] for area in self.data["areas"]][-1:], [True])
        self.assertEqual(sum(area["realm"] for area in self.data["areas"]), 1)

    def test_walls_and_doors_come_from_the_edge_table(self):
        kinds = {edge["kind"] for edge in self.data["edges"]}
        self.assertEqual(kinds, {"wall", "door", "hidden"})
        self.assertEqual(sum(edge["kind"] == "hidden" for edge in self.data["edges"]),
                         len(self.data["hidden_doors"]))

    def test_it_is_json(self):
        json.dumps(self.data)


class TrackerContextTest(unittest.IsolatedAsyncioTestCase):
    """The context, connected as the server would connect it."""

    slot_data: dict

    @classmethod
    def setUpClass(cls) -> None:
        cls.slot_data = generate({}).worlds[1].fill_slot_data()

    async def asyncSetUp(self) -> None:
        self.ctx = TrackerContext(None, None, "Soma")
        self.addCleanup(self.ctx.close)
        self.changes = 0
        self.ctx.listeners.append(self.count_change)
        ids = set(location_name_to_id.values())
        self.ctx.slot, self.ctx.team = 1, 0
        self.ctx.slot_info = {1: NetworkSlot("Soma", CVAOSWorld.game, SlotType.player)}
        self.ctx.server_locations = set(ids)
        self.ctx.missing_locations = set(ids)
        self.ctx.checked_locations = set()
        self.ctx.on_package("Connected", {"slot_data": self.slot_data})

    def count_change(self) -> None:
        self.changes += 1

    def receive(self, *names: str) -> None:
        self.ctx.items_received += [NetworkItem(CVAOSWorld.item_name_to_id[name], -1, 1, 0)
                                    for name in names]
        self.ctx.on_package("ReceivedItems", {})

    def store(self, key: str, value) -> None:
        self.ctx.stored_data[key] = value
        self.ctx.on_package("SetReply", {"key": key, "value": value})

    def check(self, name: str) -> None:
        location_id = location_name_to_id[name]
        self.ctx.checked_locations.add(location_id)
        self.ctx.missing_locations.discard(location_id)
        self.ctx.on_package("RoomUpdate", {})

    async def test_it_rebuilds_the_seed_and_tracks_it(self):
        snapshot = self.ctx.snapshot()
        self.assertTrue(snapshot["tracking"])
        self.assertEqual(snapshot["status"], "Tracking Soma")
        statuses = set(snapshot["locations"].values())
        self.assertLessEqual(statuses, {"in_logic", "tricks", "out_of_logic"})
        self.assertIn("in_logic", statuses)

    async def test_items_put_more_in_logic(self):
        def in_logic():
            return sum(s == "in_logic" for s in self.ctx.snapshot()["locations"].values())

        before = in_logic()
        self.receive("Malphas", "Hippogryph")
        self.assertGreater(in_logic(), before)

    async def test_the_item_list_counts_what_soma_has(self):
        # The list holds the seed's progression items, which depend on its options (Progressive
        # Movement replaces the movement souls), so the test takes whatever the seed has.
        names = [item["name"] for item in self.ctx.snapshot()["items"]]
        self.assertTrue(names)
        self.receive(names[0], names[0])
        items = {item["name"]: item for item in self.ctx.snapshot()["items"]}
        self.assertEqual(items[names[0]]["count"], 2)
        self.assertEqual({items[name]["count"] for name in names[1:]}, {0})
        self.assertLessEqual({item["group"] for item in items.values()},
                             {"Souls", "Key items", "Weapons"})

    async def test_progression_from_outside_the_pool_is_listed_too(self):
        # A start inventory takes its items out of the pool, but Soma still has them.
        world = self.ctx.core.get_current_world()
        listed = {item["name"] for item in self.ctx.snapshot()["items"]}
        outside = [name for name in sorted(CVAOSWorld.item_name_to_id)
                   if name not in listed and world.create_item(name).advancement]
        if not outside:
            self.skipTest("this seed's pool holds every progression item")
        self.receive(outside[0])
        items = {item["name"]: item["count"] for item in self.ctx.snapshot()["items"]}
        self.assertEqual(items.get(outside[0]), 1)

    async def test_a_check_shows_as_checked(self):
        self.receive("Malphas")
        self.check(LUCKY_CHARM)
        self.assertEqual(self.ctx.snapshot()["locations"][str(location_name_to_id[LUCKY_CHARM])],
                         "checked")

    async def test_it_follows_soma_and_the_explored_map(self):
        self.store(self.ctx.position_key, ["003", 100, 50])
        explored = Exploration(frozenset({(0, 19), (1, 19)}), frozenset({(0, 19), (1, 19)}),
                               frozenset({2}))
        self.store(self.ctx.exploration_key, exploration.encode(explored))
        snapshot = self.ctx.snapshot()
        self.assertEqual(snapshot["position"], ["003", 100, 50])
        self.assertEqual(snapshot["exploration"],
                         {"visited": [(0, 19), (1, 19)], "remembered": [(0, 19), (1, 19)],
                          "hidden_doors": [2], "castle_maps": [], "revealed": []})
        self.assertGreater(self.changes, 0)

    async def test_a_castle_map_reveals_its_areas(self):
        explored = Exploration(frozenset(), frozenset(), frozenset(), frozenset({3}))
        self.store(self.ctx.exploration_key, exploration.encode(explored))
        snapshot = self.ctx.snapshot()["exploration"]
        self.assertEqual(snapshot["castle_maps"], [3])
        revealed = {tuple(cell) for cell in snapshot["revealed"]}
        self.assertEqual(revealed, exploration.revealed_by_castle_maps(frozenset({3})))
        self.assertTrue(revealed)

    async def test_a_route_starts_where_soma_is_and_ends_once_he_gets_there(self):
        self.receive("Malphas", "Hippogryph")
        terrace = location_name_to_id[TERRACE]
        self.ctx.set_route(terrace)
        from_start = self.ctx.snapshot()["route"]
        self.assertIsNone(from_start["start_room"], "Soma's position isn't known yet")
        self.assertEqual(from_start["steps"][0]["room"], "000")
        self.assertEqual(from_start["steps"][-1]["room"], "503")
        self.assertTrue(from_start["points"])

        self.store(self.ctx.position_key, ["013", 100, 100])
        from_soma = self.ctx.snapshot()["route"]
        self.assertEqual(from_soma["start_room"], "013")
        self.assertEqual(from_soma["steps"][0]["room"], "013")
        self.assertLess(len(from_soma["steps"]), len(from_start["steps"]))

        self.check(TERRACE)
        self.assertIsNone(self.ctx.snapshot()["route"])
        self.assertIsNone(self.ctx.snapshot()["route_target"])

    async def test_there_is_no_route_out_of_reach(self):
        self.ctx.set_route(location_name_to_id[TERRACE])
        snapshot = self.ctx.snapshot()
        self.assertEqual(snapshot["route_target"], location_name_to_id[TERRACE])
        self.assertIsNone(snapshot["route"])

    async def test_a_slot_of_another_game_is_not_tracked(self):
        ctx = TrackerContext(None, None, "Link")
        self.addCleanup(ctx.close)
        ctx.slot, ctx.team = 2, 0
        ctx.slot_info = {2: NetworkSlot("Link", "A Link to the Past", SlotType.player)}
        ctx.on_package("Connected", {"slot_data": {}})
        self.assertFalse(ctx.tracking)
        self.assertIn("A Link to the Past", ctx.snapshot()["status"])


class TrackerServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.ctx = TrackerContext(None, None, None)
        self.addCleanup(self.ctx.close)
        self.server = TrackerServer(self.ctx)
        self.port = await self.server.start("127.0.0.1", 0)
        self.addAsyncCleanup(self.server.stop)

    def get(self, path: str) -> tuple[int, str, bytes]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as response:
                return response.status, response.headers["Content-Type"], response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers["Content-Type"], error.read()

    async def fetch(self, path: str) -> tuple[int, str, bytes]:
        return await asyncio.to_thread(self.get, path)

    async def test_it_serves_the_page_and_the_map(self):
        status, content_type, body = await self.fetch("/")
        self.assertEqual((status, content_type), (200, "text/html; charset=utf-8"))
        self.assertIn(b"app.js", body)
        status, content_type, body = await self.fetch("/map.json")
        self.assertEqual((status, content_type), (200, "application/json"))
        self.assertIn("locations", json.loads(body))
        self.assertEqual((await self.fetch("/app.js"))[0], 200)
        self.assertEqual((await self.fetch("/style.css"))[0], 200)
        self.assertEqual((await self.fetch("/secret.txt"))[0], 404)

    async def test_serving_the_page_logs_no_errors(self):
        # websockets 13 logs each HTTP answer as a failed handshake unless the server quiets it.
        with self.assertNoLogs("websockets", level="ERROR"):
            await self.fetch("/")
            await asyncio.sleep(0.2)  # the library logs after the response has gone

    async def test_a_page_that_vanishes_logs_no_errors(self):
        from websockets.asyncio.client import connect
        with self.assertNoLogs("websockets", level="ERROR"):
            # A connection opened and dropped before any request, as browsers open them ahead.
            _, writer = await asyncio.open_connection("127.0.0.1", self.port)
            writer.close()
            await writer.wait_closed()
            # A page whose connection ends with no closing handshake, as a killed browser's does.
            socket = await connect(f"ws://127.0.0.1:{self.port}/ws")
            await socket.recv()
            socket.transport.abort()
            await asyncio.sleep(0.3)
        self.assertEqual(self.server.sockets, set())

    async def test_the_page_gets_a_snapshot_and_its_requests_reach_the_tracker(self):
        async with websockets.connect(f"ws://127.0.0.1:{self.port}/ws") as socket:
            message = json.loads(await socket.recv())
            self.assertEqual(message["type"], "snapshot")
            self.assertEqual(message["snapshot"]["status"], "Not connected")
            await socket.send(json.dumps({"type": "quit"}))
            await socket.send(json.dumps({"type": "nonsense"}))
            await socket.send("not json")
            await asyncio.wait_for(self.ctx.exit_event.wait(), 5)


class LaunchTest(unittest.TestCase):
    def test_the_launcher_offers_the_tracker(self):
        names = [component.display_name for component in components]
        self.assertIn("Aria of Sorrow Tracker", names)

    def test_an_archipelago_url_names_the_slot(self):
        options = parse_args(["archipelago://Soma:secret@localhost:38281"])
        self.assertEqual((options.connect, options.name, options.password),
                         ("localhost:38281", "Soma", "secret"))


if __name__ == "__main__":
    unittest.main()
