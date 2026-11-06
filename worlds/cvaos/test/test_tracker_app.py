"""
The Aria of Sorrow tracker: its copy of Universal Tracker's engine, its map data, the snapshot it
sends the page, its web server, and how the Launcher and the clients start it.

The tests run without an Archipelago server. They set the slot's details on the tracker's context
as the server would, then pass the context the packets the server would send.

    python -m pytest worlds/cvaos/test/test_tracker_app.py -v
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import pkgutil
import socket
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

from NetUtils import NetworkItem, NetworkSlot, SlotType
from websockets.exceptions import InvalidStatus
from worlds.LauncherComponents import components

from .. import CVAOSWorld, exploration, tracker_app
from ..data.room_info import by_identifier as rooms_by_identifier
from ..exploration import Exploration
from ..locations import location_name_to_id
from ..tracker_app import parse_args, serve_or_hand_over
from ..tracker_app.context import TrackerContext
from ..tracker_app.mapdata import map_data
from ..tracker_app.server import TrackerServer, build_id
from ..tracker_app.ut import UPSTREAM
from ..tracker_route import DOOR_POINTS
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
    """What the tracker's context reports once connected to a slot: location statuses, the item
    list, Soma's position, the explored map, and routes."""

    slot_data: dict

    @classmethod
    def setUpClass(cls) -> None:
        cls.slot_data = generate({}).worlds[1].fill_slot_data()

    async def asyncSetUp(self) -> None:
        self.ctx = TrackerContext(None, None, "Soma")
        self.addCleanup(self.ctx.close)
        self.changes = 0
        self.ctx.listeners.append(self.count_change)
        self.connect()

    def connect(self) -> None:
        """Set the slot's details and pass the Connected packet, as a server would."""
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
        # Movement replaces the movement souls), so the test uses whichever items the list holds.
        names = [item["name"] for item in self.ctx.snapshot()["items"]]
        self.assertTrue(names)
        self.receive(names[0], names[0])
        items = {item["name"]: item for item in self.ctx.snapshot()["items"]}
        self.assertEqual(items[names[0]]["count"], 2)
        self.assertEqual({items[name]["count"] for name in names[1:]}, {0})
        self.assertLessEqual({item["group"] for item in items.values()},
                             {"Souls", "Key items", "Weapons"})

    async def test_the_item_list_holds_the_items_item_smoothing_places(self):
        # Item smoothing holds the Ancient Books and the mobility souls out of the pool, so that it
        # can place them itself. They are listed all the same, before Soma has any of them.
        world = self.ctx.core.get_current_world()
        held_back = {item.name for item in world.get_pre_fill_items() if item.advancement}
        self.assertTrue(held_back)
        items = {item["name"]: item["count"] for item in self.ctx.snapshot()["items"]}
        self.assertEqual({name: items.get(name) for name in held_back},
                         dict.fromkeys(held_back, 0))

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

    async def test_a_position_that_is_not_a_room_and_two_numbers_is_ignored(self):
        # Any client in the room can write the position key.
        for value in ([None, 100, 50], ["003", "100", 50], ["003", 100], "003"):
            with self.subTest(value=value):
                self.store(self.ctx.position_key, value)
                self.assertIsNone(self.ctx.snapshot()["position"])

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

    async def test_a_race_room_turns_routes_off(self):
        self.receive("Malphas", "Hippogryph")
        terrace = location_name_to_id[TERRACE]
        self.ctx.set_route(terrace)
        self.assertIsNotNone(self.ctx.snapshot()["route"])
        self.assertFalse(self.ctx.snapshot()["race_mode"])

        # The flag arrives after connecting, in the server's reply to the tracker's request for it.
        self.ctx.stored_data["_read_race_mode"] = True
        self.ctx.on_package("Retrieved", {"keys": {"_read_race_mode": True}})
        snapshot = self.ctx.snapshot()
        self.assertTrue(snapshot["race_mode"])
        self.assertIsNone(snapshot["route"])
        self.assertIsNone(snapshot["route_target"])
        self.ctx.set_route(terrace)
        self.assertIsNone(self.ctx.snapshot()["route_target"])

    async def test_a_route_to_a_location_outside_the_seed_is_ignored(self):
        self.ctx.set_route(999_999_999)
        self.assertIsNone(self.ctx.snapshot()["route_target"])
        self.receive("Malphas")  # an update after it still works
        self.assertTrue(self.ctx.snapshot()["tracking"])

    async def test_a_route_to_a_location_the_rebuilt_world_lacks_is_ignored(self):
        # A seed generated by another version of the world can have a location that this
        # version's rebuild of it lacks.
        world = self.ctx.core.get_current_world()
        missing = next(location_id for location_id in CVAOSWorld.location_name_to_id.values()
                       if location_id not in {loc.address for loc in world.get_locations()})
        self.ctx.server_locations.add(missing)
        self.ctx.set_route(missing)
        self.assertIsNone(self.ctx.snapshot()["route_target"])
        self.receive("Malphas")  # an update after it still works
        self.assertTrue(self.ctx.snapshot()["tracking"])

    async def test_a_route_starts_at_the_door_soma_came_in_by(self):
        # With only Malphas, the door to 109 in room 108 is out of reach from the door to 104.
        def by_door(door: str) -> list:
            room = rooms_by_identifier["108"]
            x, y = DOOR_POINTS[door]
            return ["108", int((x - int(room.x)) * 256), int((y - int(room.y)) * 256)]

        self.receive("Malphas")
        self.store(self.ctx.position_key, by_door("108:104"))  # he comes in by the 104 door
        self.store(self.ctx.position_key, by_door("108:109"))  # and walks under the 109 door
        self.ctx.set_route(location_name_to_id["Underneath Grand Staircase [109 Silk Robe]"])
        route = self.ctx.route
        self.assertEqual(route.entrances[0].parent_region.name, "Entrance: 108:104")
        self.receive("Malphas")  # planned again on an update, still from the door he came in by
        self.assertEqual(self.ctx.route.entrances[0].parent_region.name, "Entrance: 108:104")

    async def test_a_route_survives_a_reconnect(self):
        self.receive("Malphas", "Hippogryph")
        terrace = location_name_to_id[TERRACE]
        self.ctx.set_route(terrace)
        items = list(self.ctx.items_received)
        self.ctx.reset_server_state()  # the connection ended
        self.assertEqual(self.ctx.route_target, terrace)
        self.connect()  # and came back
        self.ctx.items_received = items
        self.ctx.on_package("ReceivedItems", {})
        snapshot = self.ctx.snapshot()
        self.assertEqual(snapshot["route_target"], terrace)
        self.assertIsNotNone(snapshot["route"])

    async def test_an_item_code_from_an_older_seed_tracks_as_its_item(self):
        # Older versions gave each copy of an item its own code, using a field that is now
        # retired. The data package no longer lists those codes.
        from ..item_granting import _RETIRED_SHIFT
        code = CVAOSWorld.item_name_to_id["Malphas"] | (1 << _RETIRED_SHIFT)
        self.ctx.items_received += [NetworkItem(code, -1, 1, 0)]
        self.ctx.on_package("ReceivedItems", {})
        items = {item["name"]: item["count"] for item in self.ctx.snapshot()["items"]}
        self.assertEqual(items["Malphas"], 1)

    async def test_a_route_is_dropped_for_another_slot(self):
        self.receive("Malphas", "Hippogryph")
        self.ctx.room_address = "localhost:38281"
        self.ctx.set_route(location_name_to_id[TERRACE])
        self.ctx.connect = mock.AsyncMock()
        await self.ctx.connect_to("localhost:38281", "Alucard", None)
        self.assertIsNone(self.ctx.route_target)

    async def test_a_reply_with_the_hints_and_the_position_follows_soma(self):
        # After a reconnect, one Retrieved reply brings back both keys.
        self.ctx.stored_data[self.ctx.position_key] = ["003", 100, 50]
        self.ctx.stored_data[self.ctx.hints_key] = []
        self.ctx.on_package("Retrieved", {"keys": {self.ctx.position_key: ["003", 100, 50],
                                                   self.ctx.hints_key: []}})
        self.assertEqual(self.ctx.entry_room, "003")

    async def test_the_entry_door_is_forgotten_for_another_slot(self):
        self.store(self.ctx.position_key, ["003", 100, 50])
        self.assertEqual(self.ctx.entry_room, "003")
        self.ctx.connect = mock.AsyncMock()
        await self.ctx.connect_to("localhost:38281", "Alucard", None)
        self.assertIsNone(self.ctx.entry_room)
        self.assertIsNone(self.ctx.entry_point)

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

    def open_socket(self, origin: str | None = None):
        """Connect to the websocket as the tracker's own page does, or from ``origin``."""
        from websockets.asyncio.client import connect
        return connect(f"ws://127.0.0.1:{self.port}/ws",
                       origin=origin or f"http://127.0.0.1:{self.port}")

    async def test_a_change_during_a_push_is_pushed_too(self):
        # Sending a snapshot can take a while. A change in that time comes after the snapshot was
        # taken, so another push follows.
        sent = []
        server = self.server

        class SlowPage:
            async def send(self, message: str) -> None:
                sent.append(message)
                if len(sent) == 1:
                    server.push()  # a change while this snapshot is on its way
                await asyncio.sleep(0)

        self.server.sockets.add(SlowPage())
        self.server.push()
        await asyncio.sleep(0.5)
        self.assertEqual(len(sent), 2)

    async def test_it_serves_the_page_and_the_map(self):
        status, content_type, body = await self.fetch("/")
        self.assertEqual((status, content_type), (200, "text/html; charset=utf-8"))
        self.assertIn(b"app.js", body)
        status, content_type, body = await self.fetch("/map.json")
        self.assertEqual((status, content_type), (200, "application/json"))
        self.assertIn("locations", json.loads(body))
        # The types are exact whatever the Windows registry says, because a browser ignores a
        # stylesheet served as any other type.
        self.assertEqual((await self.fetch("/app.js"))[:2], (200, "text/javascript; charset=utf-8"))
        self.assertEqual((await self.fetch("/style.css"))[:2], (200, "text/css; charset=utf-8"))
        self.assertEqual((await self.fetch("/secret.txt"))[0], 404)

    async def test_the_map_names_the_build_that_served_the_page(self):
        # The page reads its build from map.json rather than from its websocket, which might
        # already reach a newer tracker.
        self.assertEqual(json.loads((await self.fetch("/map.json"))[2])["build"], build_id())

    def test_the_build_changes_with_the_page_s_files(self):
        from ..tracker_app import server
        before = build_id()
        build_id.cache_clear()
        self.addCleanup(build_id.cache_clear)
        with mock.patch.object(server.pkgutil, "get_data", return_value=b"another page"):
            changed = build_id()
        self.assertNotEqual(changed, before)
        self.assertEqual(changed.rsplit("/", 1)[0], before.rsplit("/", 1)[0])

    async def test_serving_the_page_logs_no_errors(self):
        # websockets 13 logs each plain HTTP response as a failed handshake, unless the server
        # filters that out.
        with self.assertNoLogs("websockets", level="ERROR"):
            await self.fetch("/")
            await asyncio.sleep(0.2)  # the library logs after the response is sent

    async def test_a_page_that_vanishes_logs_no_errors(self):
        with self.assertNoLogs("websockets", level="ERROR"):
            # A connection opened and closed before any request, as a browser opens spare
            # connections ahead of time.
            _, writer = await asyncio.open_connection("127.0.0.1", self.port)
            writer.close()
            await writer.wait_closed()
            # A page whose connection ends with no closing handshake, as a killed browser's does.
            socket = await self.open_socket()
            await socket.recv()
            socket.transport.abort()
            await asyncio.sleep(0.3)
        self.assertEqual(self.server.sockets, set())

    async def test_it_stops_once_no_page_has_been_open_a_while(self):
        watch = asyncio.create_task(self.server.stop_when_idle(limit=0.3, check=0.05))
        async with self.open_socket() as page:
            await page.recv()
            await asyncio.sleep(0.5)
            self.assertFalse(self.ctx.exit_event.is_set(), "it stopped with a page open")
        await asyncio.wait_for(self.ctx.exit_event.wait(), 2)
        await watch

    async def ask(self, request: dict) -> str:
        async with self.open_socket() as launch:
            await launch.recv()
            await launch.send(json.dumps({"type": "handover", **request}))
            answer = json.loads(await launch.recv())["result"]
        await asyncio.sleep(0.1)
        return answer

    async def test_a_launch_asking_doesn_t_restart_the_idle_wait(self):
        idle_since = self.server.idle_since
        request = {"server": "localhost:38281", "slot": "Soma", "take": False}
        self.assertEqual(await self.ask(request), "idle")
        self.assertEqual(self.server.idle_since, idle_since)
        # A page connecting does restart it, because someone is looking at the tracker.
        async with self.open_socket() as page:
            await page.recv()
        await asyncio.sleep(0.1)
        self.assertGreater(self.server.idle_since, idle_since)

    async def test_a_launch_that_opens_this_tracker_restarts_the_idle_wait(self):
        # The launch's page is about to load, so the tracker mustn't stop in the meantime.
        idle_since = self.server.idle_since
        self.assertEqual(await self.ask({"server": "", "slot": ""}), "opened")
        self.assertGreater(self.server.idle_since, idle_since)

    async def test_another_site_can_t_reach_the_tracker(self):
        # A websocket from any other origin is refused, even one on this computer.
        for origin in ("https://example.com", f"http://127.0.0.1:{self.port + 1}", "null"):
            with self.subTest(origin=origin), self.assertRaises(InvalidStatus) as refused:
                async with self.open_socket(origin):
                    pass
            self.assertEqual(refused.exception.response.status_code, 403)

    async def test_a_request_for_another_host_is_refused(self):
        # DNS rebinding points another site's name at this computer. The request's Host header
        # still carries that name, so the tracker refuses any host but its own.
        def get_as(host: str) -> int:
            request = urllib.request.Request(f"http://127.0.0.1:{self.port}/map.json",
                                             headers={"Host": host})
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                return error.code

        self.assertEqual(await asyncio.to_thread(get_as, f"evil.example:{self.port}"), 403)
        self.assertEqual(await asyncio.to_thread(get_as, f"localhost:{self.port}"), 200)

    async def test_a_request_that_fails_leaves_the_page_connected(self):
        self.ctx.set_route = mock.Mock(side_effect=RuntimeError("a bug"))
        async with self.open_socket() as page:
            await page.recv()
            with self.assertLogs("Client", level="ERROR"):
                await page.send(json.dumps({"type": "route", "location": 1}))
                await asyncio.sleep(0.2)
            await page.send(json.dumps({"type": "quit"}))
            await asyncio.wait_for(self.ctx.exit_event.wait(), 5)

    async def test_the_page_gets_a_snapshot_and_its_requests_reach_the_tracker(self):
        async with self.open_socket() as socket:
            message = json.loads(await socket.recv())
            self.assertEqual(message["type"], "snapshot")
            self.assertEqual((message["app"], message["build"]), ("cvaos-tracker", build_id()))
            self.assertEqual(message["snapshot"]["status"], "Not connected")
            # The tracker ignores requests it can't use, and the page stays connected for the next.
            await socket.send(json.dumps({"type": "nonsense"}))
            await socket.send("not json")
            await socket.send(json.dumps({"type": "route", "location": "the castle"}))
            await socket.send(json.dumps({"type": "route"}))
            await socket.send(json.dumps({"type": "quit"}))
            await asyncio.wait_for(self.ctx.exit_event.wait(), 5)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class RefusedConnectionTest(unittest.IsolatedAsyncioTestCase):
    """What the page says when the tracker can't log in to the room."""

    async def log_in(self, slot_name: str | None, password_requested: bool) -> str:
        ctx = TrackerContext(None, None, slot_name)
        self.addCleanup(ctx.close)

        async def connection() -> None:
            await ctx.server_auth(password_requested)

        # The login happens inside the connection, so server_auth runs in the connection's own
        # task, as it does under CommonContext's server loop.
        ctx.server_task = asyncio.create_task(connection())
        await ctx.server_task
        await asyncio.sleep(0)  # let the disconnect that server_auth started run
        return ctx.snapshot()["status"]

    async def test_a_room_that_needs_a_password_says_so(self):
        # A room link carries no password, so a room that has one refuses the first login.
        self.assertEqual(await self.log_in("Soma", True), "This room needs a password")

    async def test_a_missing_slot_name_says_so(self):
        self.assertEqual(await self.log_in(None, False), "Enter a slot name to connect")


class OpeningConnectionTest(unittest.IsolatedAsyncioTestCase):
    """Connecting or disconnecting while a connection is still opening, as to a slow server."""

    async def asyncSetUp(self) -> None:
        self.ctx = TrackerContext(None, None, "Soma")
        self.addCleanup(self.ctx.close)
        self.addAsyncCleanup(self.ctx.leave)
        never = asyncio.Event()

        async def still_opening(ctx, address=None) -> None:
            await never.wait()

        patcher = mock.patch("CommonClient.server_loop", still_opening)
        patcher.start()
        self.addCleanup(patcher.stop)

    def live_connections(self) -> list:
        return [task for task in asyncio.all_tasks()
                if task.get_name() == "server loop" and not task.done()]

    async def test_disconnecting_ends_it(self):
        await self.ctx.connect_to("localhost:38281", "Soma", None)
        opening = self.ctx.server_task
        await asyncio.wait_for(self.ctx.leave(), timeout=2)
        self.assertTrue(opening.cancelled())
        self.assertEqual(self.live_connections(), [])
        self.assertFalse(self.ctx.will_reconnect)

    async def test_connecting_again_replaces_it(self):
        await self.ctx.connect_to("localhost:38281", "Soma", None)
        opening = self.ctx.server_task
        await asyncio.wait_for(self.ctx.connect_to("localhost:38282", "Soma", None), timeout=2)
        self.assertTrue(opening.cancelled())
        self.assertEqual(self.live_connections(), [self.ctx.server_task])

    async def test_two_requests_while_a_connection_closes_open_one_connection(self):
        # Closing an open connection takes a moment. Both requests wait for it, and only one of
        # them may then open a connection.
        closed = asyncio.Event()

        async def close() -> None:
            await asyncio.sleep(0.05)
            closed.set()

        async def open_connection() -> None:
            await closed.wait()
            self.ctx.server = None  # as connection_closed() does once the socket closes

        self.ctx.server = SimpleNamespace(socket=SimpleNamespace(closed=False, close=close))
        self.ctx.server_task = asyncio.create_task(open_connection())
        await asyncio.wait_for(asyncio.gather(
            self.ctx.connect_to("localhost:38281", "Soma", None),
            self.ctx.connect_to("localhost:38282", "Soma", None)), timeout=2)
        self.assertEqual(self.live_connections(), [self.ctx.server_task])


def following():
    """Make ``tracking`` true, as for a tracker that has rebuilt its slot's seed."""
    return mock.patch.object(TrackerContext, "tracking", new_callable=mock.PropertyMock,
                             return_value=True)


class ReconnectStatusTest(unittest.IsolatedAsyncioTestCase):
    """What the page says when the tracker's connection ends."""

    async def asyncSetUp(self) -> None:
        self.ctx = TrackerContext(None, None, "Soma")
        self.addCleanup(self.ctx.close)
        self.ctx.server_address, self.ctx.username = "ws://localhost:38281", "Soma"
        self.ctx.room_address = "localhost:38281"
        self.ctx.disconnected_intentionally = False
        self.ctx.status = "Tracking Soma"

    async def test_a_connection_that_will_come_back_says_so(self):
        # The server closed the connection, and CommonContext reconnects on its own.
        self.ctx.reset_server_state()
        self.assertEqual(self.ctx.status, "Reconnecting to localhost:38281")

    async def test_a_disconnect_just_ends(self):
        self.ctx.disconnected_intentionally = True
        self.ctx.reset_server_state()
        self.assertEqual(self.ctx.status, "Not connected")

    async def test_a_lost_connection_says_it_tries_again_and_not_to_type_connect(self):
        with self.assertLogs("Client", level="ERROR"):
            self.ctx.handle_connection_loss(
                "Lost connection to the multiworld server, type /connect to reconnect")
        self.assertEqual(self.ctx.status,
                         "Lost connection to the multiworld server. Trying again shortly")
        self.assertTrue(self.ctx.snapshot()["following"] is self.ctx.following)


class ShutdownTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_connection_still_opening_doesn_t_hold_up_the_shutdown(self):
        ctx = TrackerContext(None, None, "Soma")
        self.addCleanup(ctx.close)
        unanswered = asyncio.Event()  # a server that hasn't answered the connection yet
        ctx.server_task = asyncio.create_task(unanswered.wait())
        opening = ctx.server_task
        await asyncio.wait_for(ctx.shutdown(), 2)
        self.assertTrue(opening.cancelled())

    async def test_a_stopping_tracker_opens_no_new_connection(self):
        ctx = TrackerContext(None, None, None)
        self.addCleanup(ctx.close)
        ctx.connect = mock.AsyncMock()
        ctx.exit_event.set()
        await ctx.connect_to("localhost:38281", "Soma", None)
        ctx.connect.assert_not_awaited()


class HandOverTest(unittest.IsolatedAsyncioTestCase):
    """How a running tracker answers a new launch that asks it for a room and slot."""

    async def asyncSetUp(self) -> None:
        self.ctx = TrackerContext(None, None, None)
        self.addCleanup(self.ctx.close)
        self.ctx.connect_to = mock.AsyncMock()

    def connect_as(self, slot_name: str, address: str) -> None:
        self.ctx.slot, self.ctx.slot_name, self.ctx.room_address = 1, slot_name, address

    async def test_an_idle_tracker_takes_the_slot(self):
        self.assertEqual(await self.ctx.hand_over("localhost:38281", "Soma", "pw"), "opened")
        self.ctx.connect_to.assert_awaited_once_with("localhost:38281", "Soma", "pw")

    async def test_a_launch_that_asks_for_nothing_just_opens_the_page(self):
        self.connect_as("Link", "localhost:38281")
        self.assertEqual(await self.ctx.hand_over("", "", None), "opened")
        self.ctx.connect_to.assert_not_awaited()

    async def test_the_same_room_and_slot_just_open_the_page(self):
        self.connect_as("Soma", "localhost:38281")
        with following():
            self.assertEqual(await self.ctx.hand_over("ws://LOCALHOST:38281", "Soma", None),
                             "opened")
        self.ctx.connect_to.assert_not_awaited()

    async def test_another_room_or_slot_finds_it_busy(self):
        self.connect_as("Soma", "localhost:38281")
        with following():
            self.assertEqual(await self.ctx.hand_over("localhost:38282", "Soma", None), "busy")
            self.assertEqual(await self.ctx.hand_over("localhost:38281", "Link", None), "busy")
        self.ctx.connect_to.assert_not_awaited()

    async def test_a_tracker_still_connecting_keeps_its_room(self):
        # Before the server sends Connected, the tracker has no slot yet, but the room it is
        # connecting to already counts as its own.
        self.ctx.slot_name, self.ctx.room_address = "Soma", "localhost:38281"
        self.ctx.server_task = asyncio.get_running_loop().create_future()
        self.addCleanup(self.ctx.server_task.cancel)
        self.assertEqual(await self.ctx.hand_over("localhost:38282", "Link", None), "busy")
        self.assertEqual(await self.ctx.hand_over("localhost:38281", "Soma", None), "opened")
        self.ctx.connect_to.assert_not_awaited()

    async def test_a_tracker_that_can_t_follow_its_slot_takes_another(self):
        # The tracker is connected but not tracking, as with another game's slot or a seed it
        # couldn't rebuild.
        self.connect_as("Link", "localhost:38281")
        self.assertEqual(await self.ctx.hand_over("localhost:38281", "Soma", None), "opened")
        self.ctx.connect_to.assert_awaited_once_with("localhost:38281", "Soma", None)

    async def test_an_idle_tracker_waits_until_the_launch_lets_it_take_the_slot(self):
        self.assertEqual(await self.ctx.hand_over("localhost:38281", "Soma", None, take=False),
                         "idle")
        self.ctx.connect_to.assert_not_awaited()

    async def test_a_tracker_waiting_to_reconnect_keeps_its_room(self):
        # An archipelago.gg room that went to sleep: the tracker retries until the room wakes.
        self.ctx.slot_name, self.ctx.room_address = "Alucard", "archipelago.gg:38281"
        self.ctx.autoreconnect_task = asyncio.get_running_loop().create_future()
        self.addCleanup(self.ctx.autoreconnect_task.cancel)
        self.assertEqual(await self.ctx.hand_over("localhost:38281", "Soma", None), "busy")
        self.ctx.connect_to.assert_not_awaited()

    async def test_its_own_slot_again_waits_to_be_taken(self):
        # The room refused this tracker for a missing password, and a launch now asks for the
        # same slot with the password. The tracker answers that it remembers the slot, then
        # reconnects only when a request says to take it. That way it never follows a slot that
        # another tracker already follows.
        self.ctx.slot_name, self.ctx.room_address = "Soma", "localhost:38281"
        self.assertEqual(await self.ctx.hand_over("localhost:38281", "Soma", "pw", take=False),
                         "remembers")
        self.ctx.connect_to.assert_not_awaited()
        self.assertEqual(await self.ctx.hand_over("localhost:38281", "Soma", "pw"), "opened")
        self.ctx.connect_to.assert_awaited_once_with("localhost:38281", "Soma", "pw")

    async def test_a_room_that_came_back_on_another_port_is_followed_there(self):
        # archipelago.gg wakes a sleeping room on a new port, while the tracker keeps retrying
        # the old one.
        self.ctx.slot_name, self.ctx.room_address = "Soma", "archipelago.gg:12345"
        self.ctx.autoreconnect_task = asyncio.get_running_loop().create_future()
        self.addCleanup(self.ctx.autoreconnect_task.cancel)
        self.assertEqual(await self.ctx.hand_over("archipelago.gg:54321", "Soma", None, take=False),
                         "remembers")
        self.assertEqual(await self.ctx.hand_over("archipelago.gg:54321", "Soma", None), "opened")
        self.ctx.connect_to.assert_awaited_once_with("archipelago.gg:54321", "Soma", None)

    async def test_another_room_on_the_same_server_is_still_another_room(self):
        # Two rooms on one host can both have a slot named Soma.
        self.connect_as("Soma", "localhost:38281")
        with following():
            self.assertEqual(await self.ctx.hand_over("localhost:38282", "Soma", None), "busy")
        self.ctx.connect_to.assert_not_awaited()

    async def test_a_tracker_that_is_stopping_says_so(self):
        self.ctx.exit_event.set()
        self.assertEqual(await self.ctx.hand_over("localhost:38281", "Soma", None), "closing")
        self.ctx.connect_to.assert_not_awaited()


class OldTrackerServer(TrackerServer):
    def snapshot_message(self) -> str:
        message = json.loads(super().snapshot_message())
        message["build"] = "0/2020.1.1"
        return json.dumps(message)


class PortProbeTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_port_reads_free_once_its_tracker_stops_and_taken_while_it_runs(self):
        from websockets.asyncio.client import connect
        ctx = TrackerContext(None, None, None)
        self.addCleanup(ctx.close)
        server = TrackerServer(ctx)
        port = await server.start("127.0.0.1", 0)
        self.assertFalse(tracker_app.port_is_free(port))
        # A page still connected when the server stops leaves the server's end of the connection
        # in TIME_WAIT. On Linux and macOS, a plain bind to that port then fails for a minute.
        page = await connect(f"ws://127.0.0.1:{port}/ws", origin=f"http://127.0.0.1:{port}")
        await page.recv()
        await server.stop()
        await page.close()
        self.assertTrue(tracker_app.port_is_free(port))


class SecondLaunchTest(unittest.IsolatedAsyncioTestCase):
    """What a new launch does when trackers may already serve on its ports: which running tracker
    it hands the slot to, or which port it serves on itself."""

    async def run_tracker(self, server_class: type[TrackerServer] = TrackerServer):
        ctx = TrackerContext(None, None, None)
        self.addCleanup(ctx.close)
        ctx.connect_to = mock.AsyncMock()
        server = server_class(ctx)
        port = await server.start("127.0.0.1", 0)
        self.addAsyncCleanup(server.stop)
        return ctx, server, port

    async def launch(self, *args: str) -> int | None:
        server = TrackerServer(TrackerContext(None, None, None))
        self.addCleanup(server.ctx.close)
        self.addAsyncCleanup(server.stop)
        return await serve_or_hand_over(server, parse_args([*args, "--no-browser"]))

    async def test_it_opens_the_running_tracker_and_hands_it_the_slot(self):
        ctx, _, port = await self.run_tracker()
        self.assertIsNone(await self.launch("--port", str(port),
                                            "--connect", "localhost:38281", "--name", "Soma"))
        ctx.connect_to.assert_awaited_once_with("localhost:38281", "Soma", None)

    async def test_a_tracker_busy_with_another_slot_keeps_its_port(self):
        ctx, _, port = await self.run_tracker()
        ctx.slot, ctx.slot_name, ctx.room_address = 1, "Link", "localhost:38281"
        spare = free_port()
        with mock.patch.object(tracker_app, "DEFAULT_PORT", port), \
                mock.patch.object(tracker_app, "SPARE_PORTS", [spare]), following():
            self.assertEqual(await self.launch("--connect", "localhost:38281", "--name", "Soma"),
                             spare)
        ctx.connect_to.assert_not_awaited()

    async def test_the_slot_s_tracker_is_found_past_a_free_port(self):
        # The default port is free because another room's tracker stopped, but this slot's
        # tracker still serves on a spare port. The launch opens that tracker's page.
        ctx, _, port = await self.run_tracker()
        ctx.slot, ctx.slot_name, ctx.room_address = 1, "Soma", "localhost:38281"
        with mock.patch.object(tracker_app, "DEFAULT_PORT", free_port()), \
                mock.patch.object(tracker_app, "SPARE_PORTS", [port]), following():
            self.assertIsNone(await self.launch("--connect", "localhost:38281", "--name", "Soma"))

    async def test_the_slot_s_tracker_is_found_before_an_idle_one_is_given_the_slot(self):
        idle, _, idle_port = await self.run_tracker()
        follower, _, follower_port = await self.run_tracker()
        follower.slot, follower.slot_name, follower.room_address = 1, "Soma", "localhost:38281"

        def follows(ctx: TrackerContext) -> bool:
            return ctx is follower

        # The idle tracker holds the earlier port, but the launch opens the follower's page.
        with mock.patch.object(tracker_app, "DEFAULT_PORT", idle_port), \
                mock.patch.object(tracker_app, "SPARE_PORTS", [follower_port]), \
                mock.patch.object(TrackerContext, "following", property(follows)):
            self.assertIsNone(await self.launch("--connect", "localhost:38281", "--name", "Soma"))
        idle.connect_to.assert_not_awaited()
        follower.connect_to.assert_not_awaited()

    async def test_an_idle_tracker_is_given_the_slot_before_a_port_is_taken(self):
        idle, _, idle_port = await self.run_tracker()
        with mock.patch.object(tracker_app, "DEFAULT_PORT", free_port()), \
                mock.patch.object(tracker_app, "SPARE_PORTS", [idle_port]):
            self.assertIsNone(await self.launch("--connect", "localhost:38281", "--name", "Soma"))
        idle.connect_to.assert_awaited_once_with("localhost:38281", "Soma", None)

    async def test_the_tracker_that_remembers_the_slot_is_given_it_first(self):
        idle, _, idle_port = await self.run_tracker()
        remembers, _, remembers_port = await self.run_tracker()
        remembers.slot_name, remembers.room_address = "Soma", "localhost:38281"
        with mock.patch.object(tracker_app, "DEFAULT_PORT", idle_port), \
                mock.patch.object(tracker_app, "SPARE_PORTS", [remembers_port]):
            self.assertIsNone(await self.launch("--connect", "localhost:38281", "--name", "Soma"))
        remembers.connect_to.assert_awaited_once_with("localhost:38281", "Soma", None)
        idle.connect_to.assert_not_awaited()

    async def test_a_tracker_that_is_stopping_leaves_its_port_to_the_launch(self):
        ctx, server, port = await self.run_tracker()
        ctx.exit_event.set()

        async def stop_soon() -> None:
            await asyncio.sleep(0.3)
            await server.stop()

        stopping = asyncio.create_task(stop_soon())
        self.assertEqual(await self.launch("--port", str(port), "--connect", "localhost:38281",
                                           "--name", "Soma"), port)
        await stopping
        ctx.connect_to.assert_not_awaited()

    async def test_a_tracker_from_another_version_is_replaced(self):
        ctx, server, port = await self.run_tracker(OldTrackerServer)

        async def stop_once_asked() -> None:
            await ctx.exit_event.wait()
            await server.stop()

        stopping = asyncio.create_task(stop_once_asked())
        self.assertEqual(await self.launch("--port", str(port)), port)
        await stopping

    async def test_another_program_on_the_port_is_left_alone(self):
        async def not_a_tracker(_reader, writer) -> None:
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()

        other = await asyncio.start_server(not_a_tracker, "127.0.0.1", 0)
        self.addAsyncCleanup(other.wait_closed)
        self.addCleanup(other.close)
        port, spare = other.sockets[0].getsockname()[1], free_port()
        with mock.patch.object(tracker_app, "DEFAULT_PORT", port), \
                mock.patch.object(tracker_app, "SPARE_PORTS", [spare]):
            self.assertEqual(await self.launch(), spare)

        # With no spare port to move to, the launch serves on whatever port the system assigns.
        with mock.patch.object(tracker_app, "DEFAULT_PORT", port), \
                mock.patch.object(tracker_app, "SPARE_PORTS", []):
            self.assertNotIn(await self.launch(), (port, None))


class OpenFromClientTest(unittest.TestCase):
    """The clients' /map command, which starts the tracker for the client's slot."""

    def client(self, slot: int | None = 1, password: str | None = None) -> mock.Mock:
        return mock.Mock(slot=slot, server_address="localhost:38281", password=password,
                         slot_info={1: NetworkSlot("Soma", CVAOSWorld.game, SlotType.player)})

    def test_it_starts_the_tracker_for_the_client_s_slot(self):
        with mock.patch.object(tracker_app.subprocess, "Popen") as popen:
            tracker_app.open_for_client(self.client(password="secret"))
        command, env = popen.call_args.args[0], popen.call_args.kwargs["env"]
        self.assertEqual(command[-4:], ["Aria of Sorrow Tracker", "--",
                                        "--connect=localhost:38281", "--name=Soma"])
        # The password reaches the tracker in an environment variable, so it stays out of other
        # programs' process lists.
        self.assertNotIn("secret", command)
        self.assertEqual(env[tracker_app.PASSWORD_ENV], "secret")

    def test_a_login_in_the_client_s_address_stays_out_of_the_command(self):
        # A client connected with "/connect Soma:secret@host:port" keeps the login in its address.
        client = self.client(password="secret")
        client.server_address = "wss://Soma:secret@archipelago.gg:38281"
        with mock.patch.object(tracker_app.subprocess, "Popen") as popen:
            tracker_app.open_for_client(client)
        command = popen.call_args.args[0]
        self.assertIn("--connect=wss://archipelago.gg:38281", command)
        self.assertFalse(any("secret" in part for part in command))

    def test_a_slot_name_like_an_option_still_reaches_the_tracker(self):
        command = tracker_app.tracker_command("localhost:38281", "-Soma")
        self.assertEqual(parse_args(command[command.index("--") + 1:]).name, "-Soma")

    def test_the_password_reaches_the_tracker_and_leaves_the_environment(self):
        with mock.patch.dict(tracker_app.os.environ, {tracker_app.PASSWORD_ENV: "secret"}):
            self.assertEqual(parse_args(["--name", "Soma"]).password, "secret")
            # parse_args removes the variable, so the browser the tracker may start, and anything
            # that browser starts, don't inherit the password.
            self.assertNotIn(tracker_app.PASSWORD_ENV, tracker_app.os.environ)
        with mock.patch.dict(tracker_app.os.environ, {tracker_app.PASSWORD_ENV: ""}):
            self.assertIsNone(parse_args(["--name", "Soma"]).password)

    def test_addresses_match_whatever_login_or_scheme_they_carry(self):
        from ..tracker_app.context import same_address
        self.assertTrue(same_address("ws://Soma:pw@localhost:38281", "localhost:38281"))
        self.assertTrue(same_address("wss://archipelago.gg:38281", "archipelago.gg:38281"))
        self.assertFalse(same_address("localhost:38281", "localhost:38282"))
        # An address without a port means the default port, and localhost, 127.0.0.1 and [::1]
        # all name this computer.
        self.assertTrue(same_address("ws://localhost", "localhost:38281"))
        self.assertTrue(same_address("127.0.0.1:38281", "localhost:38281"))
        self.assertTrue(same_address("[::1]:38281", "localhost"))
        self.assertFalse(same_address("archipelago.gg", "localhost"))

    def test_a_room_link_to_an_ipv6_address_keeps_its_brackets(self):
        self.assertEqual(parse_args(["archipelago://Soma:@[::1]:38281"]).connect, "[::1]:38281")

    def test_a_slot_of_another_game_gets_no_tracker(self):
        # The BizHawk Client keeps /map and the "Open map" button after a ROM for another system is
        # loaded, because it asks this world only about GBA ROMs. So the tracker starts only for an
        # Aria of Sorrow slot.
        client = self.client()
        client.slot_info = {1: NetworkSlot("Link", "A Link to the Past", SlotType.player)}
        with mock.patch.object(tracker_app.subprocess, "Popen") as popen, \
                self.assertLogs("Client", level="INFO") as logs:
            tracker_app.open_for_client(client)
        popen.assert_not_called()
        self.assertIn("A Link to the Past", logs.output[0])

    def test_before_connecting_it_says_to_connect_first(self):
        with mock.patch.object(tracker_app.subprocess, "Popen") as popen, \
                self.assertLogs("Client", level="INFO") as logs:
            tracker_app.open_for_client(self.client(slot=None))
        popen.assert_not_called()
        self.assertIn("Connect to the room first", logs.output[0])

    def test_bizhawk_offers_map_only_while_an_aria_of_sorrow_rom_is_loaded(self):
        from ..client import CVAOSClient, cmd_map
        ctx = mock.Mock()
        ctx.command_processor.commands = {}
        client = CVAOSClient()
        for valid in (True, False):
            checked = mock.AsyncMock(return_value=valid)
            with self.subTest(valid=valid), \
                    mock.patch.object(CVAOSClient, "_validate_rom", checked):
                self.assertEqual(asyncio.run(client.validate_rom(ctx)), valid)
                self.assertEqual(ctx.command_processor.commands.get("map"),
                                 cmd_map if valid else None)

    def test_the_collection_client_offers_map(self):
        from ..collection_client import CVAOSCollectionContext
        self.assertIn("map", CVAOSCollectionContext.command_processor.commands)


class MapButtonTest(unittest.TestCase):
    """The "Open map" button that the clients add after "Connect" in their windows."""

    def window(self) -> SimpleNamespace:
        return SimpleNamespace(connect_layout=mock.Mock(),
                               server_connect_bar=SimpleNamespace(height=30))

    def setUp(self) -> None:
        # KivyMD widgets need a running app, so the tests replace the button with a stand-in.
        patcher = mock.patch.object(tracker_app, "map_button",
                                    side_effect=lambda ctx, height: object())
        self.map_button = patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_is_added_once(self):
        ctx = SimpleNamespace(ui=self.window())
        tracker_app.add_map_button(ctx)
        tracker_app.add_map_button(ctx)  # the server sends Connected again on every reconnect
        ctx.ui.connect_layout.add_widget.assert_called_once_with(ctx.ui.cvaos_map_button)

    def test_a_client_without_a_window_gets_none(self):
        tracker_app.add_map_button(SimpleNamespace(ui=None))
        tracker_app.add_map_button(SimpleNamespace())
        self.map_button.assert_not_called()

    def test_a_window_it_can_t_build_in_only_loses_the_button(self):
        ctx = SimpleNamespace(ui=self.window())
        self.map_button.side_effect = TypeError("a kvui with other buttons")
        with self.assertLogs("Client", level="ERROR"):
            tracker_app.add_map_button(ctx)
        self.assertIsNone(getattr(ctx.ui, "cvaos_map_button", None))

    def test_bizhawk_adds_it_on_connecting_and_drops_it_for_another_rom(self):
        from ..client import CVAOSClient
        ctx = SimpleNamespace(ui=self.window(), command_processor=SimpleNamespace(commands={}))
        client = CVAOSClient()
        client.on_package(ctx, "Connected", {"slot_data": {}})
        button = ctx.ui.cvaos_map_button
        ctx.ui.connect_layout.add_widget.assert_called_once_with(button)
        with mock.patch.object(CVAOSClient, "_validate_rom", mock.AsyncMock(return_value=False)):
            asyncio.run(client.validate_rom(ctx))
        ctx.ui.connect_layout.remove_widget.assert_called_once_with(button)
        self.assertIsNone(ctx.ui.cvaos_map_button)


class LaunchTest(unittest.TestCase):
    def test_the_launcher_offers_the_tracker(self):
        names = [component.display_name for component in components]
        self.assertIn("Aria of Sorrow Tracker", names)

    def test_an_archipelago_url_names_the_slot(self):
        options = parse_args(["archipelago://Soma:secret@localhost:38281"])
        self.assertEqual((options.connect, options.name, options.password),
                         ("localhost:38281", "Soma", "secret"))

    def test_a_room_link_offers_the_tracker(self):
        # The room page's link, as WebHostLib's macros.html writes it, names the game.
        from Launcher import handle_uri
        link = "archipelago://Soma:None@archipelago.gg:38281?game=Castlevania - Aria of Sorrow"
        offered, _ = handle_uri(link)
        self.assertIn("Aria of Sorrow Tracker", [component.display_name for component in offered])
        options = parse_args([link])
        self.assertEqual((options.connect, options.name), ("archipelago.gg:38281", "Soma"))


if __name__ == "__main__":
    unittest.main()
