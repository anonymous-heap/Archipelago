"""
The tracker's connection to the Archipelago server, and what it knows about the slot it tracks.

It connects as a tracker (it takes no items and sends no checks), rebuilds this world from the
slot data with Universal Tracker's engine, and works out which locations are in logic whenever
items arrive or checks are made. The client keeps Soma's position and the explored map in data
storage, and the tracker follows both. :meth:`TrackerContext.snapshot` is everything the web page
shows, and listeners hear when it may have changed.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import urllib.parse
from collections import deque
from typing import Callable

from CommonClient import CommonContext
from NetUtils import HintStatus
from Utils import async_start

from .. import CVAOSWorld, exploration, tracker_map, tracker_route
from ..data import item_info_collection
from ..regions import UT_TRICKS_ITEM
from . import without_login
from .ut import CurrentTrackerState
from .ut.TrackerCore import TrackerCore

logger = logging.getLogger("Client")

LOG_LINES = 100  # how many of the client log's lines the page can show
# The server's flag for a race room, which every client asks for once it has connected. Universal
# Tracker turns its routes and explanations off in a race, so this tracker turns its routes off.
RACE_MODE_KEY = "_read_race_mode"

# The page lists the progression items in these groups. Anything else is a key item.
ITEM_CATEGORIES = {info.name: info.item_category for info in item_info_collection}
ITEM_GROUPS = {"ability_soul": "Souls", "red_soul": "Souls", "blue_soul": "Souls",
               "yellow_soul": "Souls", "weapon": "Weapons"}


DEFAULT_SERVER_PORT = 38281  # the port Archipelago assumes when an address leaves it out
LOOPBACK_NAMES = {"127.0.0.1", "::1"}  # the names of this computer that "localhost" also means


def server_of(address: str) -> tuple[str, int | str]:
    """Return the host and port a server address names.

    The address can be written with or without a scheme, a slot name and password, or the default
    port, and "localhost" names the same host as 127.0.0.1.
    """
    address = without_login(address.strip().lower())
    address = address.removeprefix("wss://").removeprefix("ws://").rstrip("/")
    parts = urllib.parse.urlsplit(f"//{address}")
    try:
        port = parts.port or DEFAULT_SERVER_PORT
    except ValueError:
        return address, ""  # not an address urlsplit can read, so compared as written
    host = parts.hostname or ""
    return ("localhost" if host in LOOPBACK_NAMES else host), port


def same_address(first: str, second: str) -> bool:
    """Whether two server addresses name the same server, however each is written."""
    return server_of(first) == server_of(second)


class LogTail(logging.Handler):
    """Keeps the client log's latest lines, which the page shows under the map."""

    def __init__(self, on_line: Callable[[], None]) -> None:
        super().__init__(logging.INFO)
        self.lines: deque[str] = deque(maxlen=LOG_LINES)
        self.on_line = on_line

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())
        self.on_line()


class TrackerContext(CommonContext):
    game = ""  # a tracker plays no game
    tags = CommonContext.tags | {"Tracker"}
    items_handling = 0b111  # every item, including the slot's own, so logic sees all of them
    want_slot_data = True

    def __init__(self, server_address: str | None = None, password: str | None = None,
                 slot_name: str | None = None) -> None:
        super().__init__(server_address, password)
        self.slot_name = slot_name  # kept here, because a lost connection clears auth
        self.room_address = server_address  # the address asked for, as given
        self.core = TrackerCore(logger, False, False)
        self.result: CurrentTrackerState | None = None
        self.listeners: list[Callable[[], None]] = []
        self.status = "Not connected"
        self.route_target: int | None = None  # the location id a route was asked for
        self.route: tracker_route.Route | None = None
        self.route_room: str | None = None    # the room the route was planned from
        self.log_tail = LogTail(self.changed)
        logger.addHandler(self.log_tail)

    # --- connecting -----------------------------------------------------------------------------

    async def server_auth(self, password_requested: bool = False) -> None:
        # This runs inside the connection's own task, which disconnect() waits for, so the
        # disconnect runs beside it. Awaiting it here would fail and report a lost connection.
        if password_requested and not self.password:
            self.set_status("This room needs a password")
            async_start(self.disconnect())
            return
        if not self.slot_name:
            self.set_status("Enter a slot name to connect")
            async_start(self.disconnect())
            return
        self.auth = self.slot_name
        await self.send_connect(game="")

    async def connect_to(self, address: str, slot_name: str, password: str | None) -> None:
        """Connect to the server at ``address`` and track ``slot_name``, unless stopping."""
        if self.exit_event.is_set():
            return
        self.slot_name = slot_name
        self.room_address = address
        self.username = slot_name  # CommonContext reconnects on its own only when this is set
        self.password = password or None
        await self.connect(address)
        # After connect(), since ending the last connection sets the status to "Not connected".
        self.set_status(f"Connecting to {address}")

    @property
    def following(self) -> bool:
        """Whether this tracker follows a room and slot, is connecting to one, or will reconnect."""
        connecting = (self.slot is None and self.server_task is not None
                      and not self.server_task.done())
        reconnecting = self.autoreconnect_task is not None and not self.autoreconnect_task.done()
        return self.tracking or connecting or reconnecting

    async def hand_over(self, address: str, slot_name: str, password: str | None,
                        take: bool = True) -> str:
        """Answer a new launch that asks for a room and slot.

        Returns "opened" when the launch only needs to open this tracker's page, because the
        tracker follows that room and slot or has just taken them. Returns "busy" when it follows
        another, and "closing" when it is shutting down, so that the launch serves on the port
        itself once it comes free.

        Without ``take``, a tracker that could take the request says so instead: "remembers" when
        the slot is the one it last followed, and "idle" otherwise. A launch asks every tracker
        without ``take`` first, so that a tracker that already follows the slot opens rather than
        another being given it too. Then it gives the request to one that remembers the slot,
        before any other.

        A tracker that follows nothing could take any request. That is a tracker whose connection
        was refused, or whose slot plays another game or couldn't be rebuilt. So could one still
        trying to reconnect to the slot on the same host, since archipelago.gg brings a room that
        went to sleep back on a new port.
        """
        if self.exit_event.is_set():
            return "closing"
        if not address or not slot_name:
            return "opened"  # the launch asked for nothing but the page
        room = self.room_address or self.server_address or ""
        own_slot = slot_name == self.slot_name
        if self.following and own_slot and same_address(address, room):
            return "opened"
        moved = own_slot and not self.tracking and server_of(address)[0] == server_of(room)[0]
        if self.following and not moved:
            return "busy"
        if not take:
            return "remembers" if own_slot else "idle"
        await self.connect_to(address, slot_name, password)
        return "opened"

    def handle_connection_loss(self, msg: str) -> None:
        super().handle_connection_loss(msg)
        self.set_status(msg)

    def event_invalid_slot(self) -> None:
        # CommonContext raises here, which would report a lost connection instead of the reason.
        self.set_status(f"The server has no slot named {self.slot_name}")
        async_start(self.disconnect())

    def event_invalid_game(self) -> None:
        self.set_status("The server refused the connection")
        async_start(self.disconnect())

    async def shutdown(self) -> None:
        # CommonContext waits for the connection's task, which goes on while a connection is still
        # opening, and logs in if that succeeds. So a connection still opening is cancelled first.
        task = self.server_task
        if task is not None and not task.done() and self.server is None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self.server_task = None
        await super().shutdown()
        self.close()

    def close(self) -> None:
        """Stop keeping the client log's lines, which the context began when it was made."""
        logger.removeHandler(self.log_tail)

    def set_status(self, status: str) -> None:
        self.status = status
        self.changed()

    def changed(self) -> None:
        for listener in self.listeners:
            listener()

    @property
    def tracking(self) -> bool:
        """Whether the tracker has rebuilt the slot's world and can say what is in logic."""
        return self.slot is not None and self.core.get_current_world() is not None

    # --- server packets ------------------------------------------------------------------------

    def on_package(self, cmd: str, args: dict) -> None:
        if cmd == "Connected":
            self.on_connected(args)
        elif cmd in ("ReceivedItems", "RoomUpdate"):
            self.update()
        elif cmd in ("SetReply", "Retrieved"):
            keys = {args["key"]} if cmd == "SetReply" else set(args.get("keys", ()))
            if RACE_MODE_KEY in keys:
                self.plan_route()  # which drops any route in a race
                self.changed()
            if self.hints_key in keys:
                self.update()
            elif keys & {self.position_key, self.exploration_key}:
                self.follow_position()
                self.changed()

    def on_connected(self, args: dict) -> None:
        game = self.slot_info[self.slot].game
        if game != CVAOSWorld.game:
            self.set_status(f"{self.slot_name} plays {game}, so this tracker can't follow it")
            self.core.disconnect()
            return
        self.set_status(f"Rebuilding {self.slot_name}'s logic")
        self.core.set_slot_params(game, self.slot, self.slot_name, self.team)
        self.core.initalize_tracker_core(CVAOSWorld, args["slot_data"])
        if self.core.get_current_world() is None:
            self.set_status("The tracker couldn't rebuild this seed's logic. See the log for why.")
            if self.core.gen_error:
                logger.error(self.core.gen_error)
            return
        self.set_notify(self.position_key, self.exploration_key, self.hints_key)
        self.set_status(f"Tracking {self.slot_name}")
        self.update()

    def reset_server_state(self) -> None:
        super().reset_server_state()
        if not hasattr(self, "core"):
            return  # CommonContext.__init__ calls this before the tracker's own state exists
        self.core.disconnect()
        self.result = None
        self.route = None
        self.route_target = None
        # A reason the connection failed stays on screen. Anything else just ends.
        if self.status.startswith(("Connecting", "Rebuilding", "Tracking")):
            self.set_status("Not connected")
        else:
            self.changed()

    # --- logic ---------------------------------------------------------------------------------

    @property
    def position_key(self) -> str:
        return tracker_map.POSITION_KEY.format(team=self.team, player=self.slot)

    @property
    def exploration_key(self) -> str:
        return exploration.EXPLORATION_KEY.format(team=self.team, player=self.slot)

    @property
    def hints_key(self) -> str:
        return f"_read_hints_{self.team}_{self.slot}"

    def hinted_locations(self) -> dict[int, int]:
        """Return the slot's own locations that a hint names, and the hint's status."""
        hints = self.stored_data.get(self.hints_key) or []
        return {hint["location"]: hint["status"] for hint in hints
                if hint["status"] not in (HintStatus.HINT_FOUND, HintStatus.HINT_AVOID)
                and self.slot_concerns_self(hint["finding_player"])}

    def update(self) -> None:
        """Work out again which locations are in logic, then tell the listeners."""
        if not self.tracking:
            return
        self.core.set_missing_locations(self.missing_locations)
        self.core.set_items_received(self.items_received)
        self.core.set_hints(self.hinted_locations())
        self.result = self.core.updateTracker()
        self.plan_route()
        self.changed()

    def location_status(self, location_id: int) -> str:
        if location_id in self.checked_locations:
            return "checked"
        if location_id in self.core.locations_available:
            return "in_logic"
        if location_id in self.core.glitched_locations:
            return "tricks"
        return "out_of_logic"

    # --- Soma and routes -----------------------------------------------------------------------

    def position(self) -> list | None:
        """Return Soma's position as the client keeps it, [room, x, y], or None before it has."""
        value = self.stored_data.get(self.position_key) if self.slot is not None else None
        try:
            tracker_map.cell_of(value)
        except (TypeError, ValueError):
            return None
        return value

    def follow_position(self) -> None:
        """Plan the route again when Soma has gone into another room, so it starts where he is."""
        position = self.position()
        if self.route_target is None or position is None:
            return
        if position[0] != self.route_room:
            self.plan_route()

    @property
    def race_mode(self) -> bool:
        """Whether the room is a race, where the tracker plans no routes."""
        return bool(self.stored_data.get(RACE_MODE_KEY))

    def set_route(self, location_id: int | None) -> None:
        """Show a route to the location with ``location_id``, or none.

        A location the seed doesn't have is ignored, since planning a route to it would fail on
        every update after.
        """
        if location_id is not None and (self.race_mode or location_id not in self.server_locations):
            return
        self.route_target = location_id
        self.plan_route()
        self.changed()

    def plan_route(self) -> None:
        """Plan the route to the route's target from Soma's room, or drop it once it is checked.

        A race room drops it too.
        """
        self.route = None
        if self.race_mode:
            self.route_target = None
            return
        if self.route_target in self.checked_locations:
            self.route_target = None  # Soma got there
        if self.route_target is None or not self.tracking or self.result is None:
            return
        world = self.core.get_current_world()
        location = world.get_location(world.location_id_to_name[self.route_target])
        position = self.position()
        self.route_room = position[0] if position is not None else None
        # The route is planned again only when Soma enters another room, so at the door he came in
        # by, which is where it starts.
        try:
            start_point = tracker_map.cell_position(*position) if position is not None else None
        except KeyError:
            start_point = None  # a room this world has no place for
        for state in (self.result.state, self.result.glitches_state):
            if state is not None:
                self.route = tracker_route.plan_route(world, location, state, self.route_room,
                                                      start_point)
                if self.route is not None:
                    return

    # --- what the page shows -------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Return everything the web page shows, as JSON values."""
        snapshot = {
            "status": self.status,
            "connected": self.slot is not None,
            "tracking": self.tracking,
            "server": self.server_address or "",
            "slot": self.slot_name or "",
            "log": list(self.log_tail.lines),
            "race_mode": self.race_mode,
        }
        if not self.tracking or self.result is None:
            return snapshot
        world = self.core.get_current_world()
        hinted = self.hinted_locations()
        locations = {location_id: self.location_status(location_id)
                     for location_id in self.server_locations}
        beaten = world.multiworld.has_beaten_game
        tricks_state = self.result.glitches_state
        if beaten(self.result.state, world.player):
            goal = "yes"
        elif tricks_state is not None and beaten(tricks_state, world.player):
            goal = "tricks"
        else:
            goal = "no"
        # The pool's progression items, and any Soma has from elsewhere, such as a start inventory.
        in_pool = {item.name for item in world.multiworld.itempool
                   if item.player == world.player and item.advancement}
        progression = sorted((in_pool | set(self.result.prog_items)) - {UT_TRICKS_ITEM})
        snapshot.update({
            "locations": {str(location_id): status for location_id, status in locations.items()},
            "hinted": sorted(location_id for location_id in hinted if location_id in locations),
            "goal": goal,
            "items": [{"name": name, "count": self.result.all_items.get(name, 0),
                       "group": ITEM_GROUPS.get(ITEM_CATEGORIES.get(name), "Key items")}
                      for name in progression],
            "position": self.position(),
            "exploration": self.exploration_snapshot(),
            "route_target": self.route_target,  # set with no route when there is no way there yet
            "route": self.route_snapshot(),
        })
        return snapshot

    def exploration_snapshot(self) -> dict | None:
        explored = exploration.decode(self.stored_data.get(self.exploration_key))
        if explored is None:
            return None
        return {"visited": sorted(explored.visited), "remembered": sorted(explored.remembered),
                "hidden_doors": sorted(explored.hidden_doors),
                "castle_maps": sorted(explored.castle_maps),
                # the cells the Castle Maps held show, which the page draws like remembered ones
                "revealed": sorted(exploration.revealed_by_castle_maps(explored.castle_maps))}

    def route_snapshot(self) -> dict | None:
        route = self.route
        if route is None:
            return None
        state = self.result.glitches_state if route.tricks else self.result.state
        steps = tracker_route.route_steps(state, route.entrances)
        return {
            "target": route.location.address,
            "start_room": route.start_room,
            "tricks": route.tricks,
            "steps": [{"room": room, "area": tracker_map.AREA_NAMES[room[0]], "needs": needs}
                      for room, needs in steps],
            "points": [list(point) for point in tracker_route.route_points(route)],
        }
