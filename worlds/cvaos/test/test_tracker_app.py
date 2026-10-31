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
from ..exploration import Exploration
from ..locations import location_name_to_id
from ..tracker_app import parse_args, serve_or_hand_over
from ..tracker_app.context import TrackerContext
from ..tracker_app.mapdata import map_data
from ..tracker_app.server import TrackerServer, build_id
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

    async def test_a_race_room_turns_routes_off(self):
        self.receive("Malphas", "Hippogryph")
        terrace = location_name_to_id[TERRACE]
        self.ctx.set_route(terrace)
        self.assertIsNotNone(self.ctx.snapshot()["route"])
        self.assertFalse(self.ctx.snapshot()["race_mode"])

        # The flag comes after connecting, as the reply to the client's request for it.
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

    async def test_it_serves_the_page_and_the_map(self):
        status, content_type, body = await self.fetch("/")
        self.assertEqual((status, content_type), (200, "text/html; charset=utf-8"))
        self.assertIn(b"app.js", body)
        status, content_type, body = await self.fetch("/map.json")
        self.assertEqual((status, content_type), (200, "application/json"))
        self.assertIn("locations", json.loads(body))
        # Exact types, whatever the computer's registry says, since a browser drops a stylesheet
        # of any other type.
        self.assertEqual((await self.fetch("/app.js"))[:2], (200, "text/javascript; charset=utf-8"))
        self.assertEqual((await self.fetch("/style.css"))[:2], (200, "text/css; charset=utf-8"))
        self.assertEqual((await self.fetch("/secret.txt"))[0], 404)

    async def test_the_map_names_the_build_that_served_the_page(self):
        # A page learns its build here, before its socket might reach a newer tracker.
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
        # websockets 13 logs each HTTP answer as a failed handshake unless the server quiets it.
        with self.assertNoLogs("websockets", level="ERROR"):
            await self.fetch("/")
            await asyncio.sleep(0.2)  # the library logs after the response has gone

    async def test_a_page_that_vanishes_logs_no_errors(self):
        with self.assertNoLogs("websockets", level="ERROR"):
            # A connection opened and dropped before any request, as browsers open them ahead.
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

    async def test_another_site_can_t_reach_the_tracker(self):
        # A page from any other origin, even one on this computer, is turned away.
        for origin in ("https://example.com", f"http://127.0.0.1:{self.port + 1}", "null"):
            with self.subTest(origin=origin), self.assertRaises(InvalidStatus) as refused:
                async with self.open_socket(origin):
                    pass
            self.assertEqual(refused.exception.response.status_code, 403)

    async def test_a_request_for_another_host_is_refused(self):
        # DNS rebinding points a site's own name at this computer, so the Host header names it.
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

    async def test_the_page_gets_a_snapshot_and_its_requests_reach_the_tracker(self):
        async with self.open_socket() as socket:
            message = json.loads(await socket.recv())
            self.assertEqual(message["type"], "snapshot")
            self.assertEqual((message["app"], message["build"]), ("cvaos-tracker", build_id()))
            self.assertEqual(message["snapshot"]["status"], "Not connected")
            # Requests it can't use are dropped, and the page stays connected for the next.
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
    """What the page says when the tracker can't log in, which happens inside the connection."""

    async def log_in(self, slot_name: str | None, password_requested: bool) -> str:
        ctx = TrackerContext(None, None, slot_name)
        self.addCleanup(ctx.close)

        async def connection() -> None:
            await ctx.server_auth(password_requested)

        # server_auth runs in the connection's own task, as the server loop runs it.
        ctx.server_task = asyncio.create_task(connection())
        await ctx.server_task
        await asyncio.sleep(0)  # the disconnect that runs beside it
        return ctx.snapshot()["status"]

    async def test_a_room_that_needs_a_password_says_so(self):
        # A room link carries no password, so a room that has one refuses the first try.
        self.assertEqual(await self.log_in("Soma", True), "This room needs a password")

    async def test_a_missing_slot_name_says_so(self):
        self.assertEqual(await self.log_in(None, False), "Enter a slot name to connect")


def following():
    """Make ``tracking`` true, as for a tracker that has rebuilt its slot's seed."""
    return mock.patch.object(TrackerContext, "tracking", new_callable=mock.PropertyMock,
                             return_value=True)


class HandOverTest(unittest.IsolatedAsyncioTestCase):
    """What a running tracker does with the room and slot a new launch brings it."""

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
        # Before the server's Connected, the slot isn't known yet, but the room is spoken for.
        self.ctx.slot_name, self.ctx.room_address = "Soma", "localhost:38281"
        self.ctx.server_task = asyncio.get_running_loop().create_future()
        self.addCleanup(self.ctx.server_task.cancel)
        self.assertEqual(await self.ctx.hand_over("localhost:38282", "Link", None), "busy")
        self.assertEqual(await self.ctx.hand_over("localhost:38281", "Soma", None), "opened")
        self.ctx.connect_to.assert_not_awaited()

    async def test_a_tracker_that_can_t_follow_its_slot_takes_another(self):
        # Connected to a slot of another game, or one whose seed couldn't be rebuilt.
        self.connect_as("Link", "localhost:38281")
        self.assertEqual(await self.ctx.hand_over("localhost:38281", "Soma", None), "opened")
        self.ctx.connect_to.assert_awaited_once_with("localhost:38281", "Soma", None)

    async def test_a_tracker_that_is_stopping_says_so(self):
        self.ctx.exit_event.set()
        self.assertEqual(await self.ctx.hand_over("localhost:38281", "Soma", None), "closing")
        self.ctx.connect_to.assert_not_awaited()


class OldTrackerServer(TrackerServer):
    def snapshot_message(self) -> str:
        message = json.loads(super().snapshot_message())
        message["build"] = "0/2020.1.1"
        return json.dumps(message)


class SecondLaunchTest(unittest.IsolatedAsyncioTestCase):
    """A launch while a tracker already serves on its port."""

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
        # The default port came free when another room's tracker stopped, but this slot's
        # tracker still serves on a spare one, and the launch opens it.
        ctx, _, port = await self.run_tracker()
        ctx.slot, ctx.slot_name, ctx.room_address = 1, "Soma", "localhost:38281"
        with mock.patch.object(tracker_app, "DEFAULT_PORT", free_port()), \
                mock.patch.object(tracker_app, "SPARE_PORTS", [port]), following():
            self.assertIsNone(await self.launch("--connect", "localhost:38281", "--name", "Soma"))

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

        # With no spare port to move to, it serves on whatever port the system gives it.
        with mock.patch.object(tracker_app, "DEFAULT_PORT", port), \
                mock.patch.object(tracker_app, "SPARE_PORTS", []):
            self.assertNotIn(await self.launch(), (port, None))


class OpenFromClientTest(unittest.TestCase):
    """/map in the game clients, which start the tracker for the slot they're connected as."""

    def client(self, slot: int | None = 1, password: str | None = None) -> mock.Mock:
        return mock.Mock(slot=slot, server_address="localhost:38281", password=password,
                         slot_info={1: NetworkSlot("Soma", CVAOSWorld.game, SlotType.player)})

    def test_it_starts_the_tracker_for_the_client_s_slot(self):
        with mock.patch.object(tracker_app.subprocess, "Popen") as popen:
            tracker_app.open_for_client(self.client(password="secret"))
        command, env = popen.call_args.args[0], popen.call_args.kwargs["env"]
        self.assertEqual(command[-4:], ["Aria of Sorrow Tracker", "--",
                                        "--connect=localhost:38281", "--name=Soma"])
        # The password goes to the tracker out of sight of other programs' process lists.
        self.assertNotIn("secret", command)
        self.assertEqual(env[tracker_app.PASSWORD_ENV], "secret")

    def test_a_login_in_the_client_s_address_stays_out_of_the_command(self):
        # A client connected with "/connect Soma:secret@host:port" keeps that in its address.
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
            # The browser the tracker may start, and what that starts, don't inherit it.
            self.assertNotIn(tracker_app.PASSWORD_ENV, tracker_app.os.environ)
        with mock.patch.dict(tracker_app.os.environ, {tracker_app.PASSWORD_ENV: ""}):
            self.assertIsNone(parse_args(["--name", "Soma"]).password)

    def test_addresses_match_whatever_login_or_scheme_they_carry(self):
        from ..tracker_app.context import same_address
        self.assertTrue(same_address("ws://Soma:pw@localhost:38281", "localhost:38281"))
        self.assertTrue(same_address("wss://archipelago.gg:38281", "archipelago.gg:38281"))
        self.assertFalse(same_address("localhost:38281", "localhost:38282"))

    def test_a_slot_of_another_game_gets_no_tracker(self):
        # After another game's ROM is loaded, /map and the button can still be there.
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
    """The "Open map" button the game clients put after Connect in their windows."""

    def window(self) -> SimpleNamespace:
        return SimpleNamespace(connect_layout=mock.Mock(),
                               server_connect_bar=SimpleNamespace(height=30))

    def setUp(self) -> None:
        # KivyMD widgets need a running app, so the button itself is a stand-in.
        patcher = mock.patch.object(tracker_app, "map_button",
                                    side_effect=lambda ctx, height: object())
        self.map_button = patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_is_added_once(self):
        ctx = SimpleNamespace(ui=self.window())
        tracker_app.add_map_button(ctx)
        tracker_app.add_map_button(ctx)  # Connected comes again on every reconnect
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
        # The room page's link, which names the game, as WebHostLib's macros.html writes it.
        from Launcher import handle_uri
        link = "archipelago://Soma:None@archipelago.gg:38281?game=Castlevania - Aria of Sorrow"
        offered, _ = handle_uri(link)
        self.assertIn("Aria of Sorrow Tracker", [component.display_name for component in offered])
        options = parse_args([link])
        self.assertEqual((options.connect, options.name), ("archipelago.gg:38281", "Soma"))


if __name__ == "__main__":
    unittest.main()
