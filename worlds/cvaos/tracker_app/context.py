"""
The tracker's connection to the Archipelago server, and the state it keeps for the slot it tracks.

It connects as a tracker, so it plays no game and sends no location checks. It rebuilds this
world from the slot data with Universal Tracker's engine, then works out again which locations
are in logic whenever items arrive or locations are checked.

The game client (the BizHawk Client or the Collection Client) keeps Soma's position and the
explored map in data storage, and the server tells the tracker whenever either changes.
:meth:`TrackerContext.snapshot` returns everything the page shows, and each listener is called
whenever the snapshot may have changed.
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
from ..item_granting import canonical_code
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
LOOPBACK_NAMES = {"127.0.0.1", "::1"}  # addresses that name the same computer as "localhost"


def server_of(address: str) -> tuple[str, int | str]:
    """Return the host and port a server address names.

    The address may carry a ws:// or wss:// scheme and a slot name and password, and it may leave
    out the default port. The host is returned as "localhost" for 127.0.0.1 and ::1 too, since all
    three name this computer.
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
    """Keeps the client log's latest lines, which the page's "Log" button shows."""

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
    items_handling = 0b111  # every item, including the slot's own, so logic counts all of them
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
        self.entry_room: str | None = None    # the room Soma is in, and where he came into it,
        self.entry_point: tuple[float, float] | None = None  # in map cells
        self.log_tail = LogTail(self.changed)
        logger.addHandler(self.log_tail)
        # Held while a request connects or disconnects. Connecting first ends the current
        # connection, which can take a while, so two requests at once (from two pages, or a page
        # and a launch) could otherwise each open a connection.
        self._connection_change = asyncio.Lock()

    # --- connecting -----------------------------------------------------------------------------

    async def server_auth(self, password_requested: bool = False) -> None:
        # This runs inside the connection's task, and disconnect() waits for that task to end.
        # Awaiting disconnect() here would fail and report a lost connection, so it runs as a
        # task of its own.
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
        """Connect to the server at ``address`` and track ``slot_name``, unless shutting down."""
        if self.exit_event.is_set():
            return
        async with self._connection_change:
            if slot_name != self.slot_name or not same_address(address, self.room_address or ""):
                # The route and the door Soma came in by belong to another AP room or slot.
                self.route_target = None
                self.entry_room = self.entry_point = None
            self.slot_name = slot_name
            self.room_address = address
            self.username = slot_name  # CommonContext reconnects on its own only when this is set
            self.password = password or None
            await self.connect(address)
            # After connect(), since ending the previous connection sets the status to
            # "Not connected".
            self.set_status(f"Connecting to {address}")

    async def leave(self) -> None:
        """End the connection, as the page's Disconnect button asks."""
        async with self._connection_change:
            await self.disconnect()

    async def disconnect(self, allow_autoreconnect: bool = False) -> None:
        # CommonContext closes the socket only once the connection is open, and then waits for the
        # connection's task. A connection still opening has no socket to close, so that wait would
        # last until the connection opened and later closed. It is cancelled instead.
        if not allow_autoreconnect:
            self.disconnected_intentionally = True  # so the cancelled task doesn't reconnect
        await self._cancel_opening_connection()
        await super().disconnect(allow_autoreconnect)

    async def _cancel_opening_connection(self) -> None:
        """Cancel the connection's task while the connection is still opening.

        Left running, that task logs in if the connection succeeds, and CommonContext would wait
        for it until the connection later closed.
        """
        task = self.server_task
        if task is not None and not task.done() and self.server is None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self.server_task = None

    @property
    def following(self) -> bool:
        """Whether this tracker is tracking a slot, connecting to one, or waiting to reconnect."""
        connecting = (self.slot is None and self.server_task is not None
                      and not self.server_task.done())
        reconnecting = self.autoreconnect_task is not None and not self.autoreconnect_task.done()
        return self.tracking or connecting or reconnecting

    async def hand_over(self, address: str, slot_name: str, password: str | None,
                        take: bool = True) -> str:
        """Answer a new launch that asks for a room and slot.

        Returns "opened" when the launch only needs to open this tracker's page. That is when the
        tracker already follows that room and slot or has just taken them, or the launch names none.
        Returns "busy" when the tracker follows another room or slot. Returns "closing" when it is
        shutting down, so that the launch can serve on the port itself once the port comes free.

        Without ``take``, a tracker that could take the request says so instead: "remembers" when
        the slot is the one it last followed, and "idle" otherwise. A launch first asks every
        tracker without ``take``, so that it finds a tracker that already follows the slot before
        giving the slot to another. It then offers the request to trackers that remember the slot
        before idle ones.

        A tracker that follows nothing can take any request. That includes a tracker whose
        connection was refused, or whose slot plays another game or couldn't be rebuilt. A tracker
        still connecting or reconnecting to the same slot on the same host can take the request
        too, because archipelago.gg brings a room that went to sleep back on a new port.
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

    @property
    def will_reconnect(self) -> bool:
        """Whether CommonContext will try the connection again by itself once it ends."""
        return bool(self.server_address and self.username and not self.disconnected_intentionally)

    def handle_connection_loss(self, msg: str) -> None:
        super().handle_connection_loss(msg)
        # CommonClient says to type /connect, which the page doesn't have.
        reason = msg.replace(", type /connect to reconnect", "").rstrip(". ")
        self.set_status(f"{reason}. Trying again shortly" if self.will_reconnect else reason)

    def event_invalid_slot(self) -> None:
        # CommonContext raises here, which would report a lost connection instead of the reason.
        self.set_status(f"The server has no slot named {self.slot_name}")
        async_start(self.disconnect())

    def event_invalid_game(self) -> None:
        self.set_status("The server refused the connection")
        async_start(self.disconnect())

    async def shutdown(self) -> None:
        await self._cancel_opening_connection()
        await super().shutdown()
        self.close()

    def close(self) -> None:
        """Stop keeping the client log's lines for the page.

        The context keeps them from the moment it is created, even if it never connects.
        """
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
            if keys & {self.position_key, self.exploration_key}:
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
            self.set_status("The tracker couldn't rebuild this seed's logic. See the log for why")
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
        self.route = None  # planned again once the seed is rebuilt, to the same target
        # A status that says why the connection failed stays on screen. "Connecting", "Rebuilding"
        # and "Tracking" are no longer true, so they become "Not connected". They become
        # "Reconnecting to ..." instead when CommonContext will try again, as it does after the
        # server closes the connection.
        if self.status.startswith(("Connecting", "Rebuilding", "Tracking")):
            room = self.room_address or self.server_address
            self.set_status(f"Reconnecting to {room}" if self.will_reconnect else "Not connected")
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
        """Return the slot's own locations that a hint names, each with that hint's status.

        Found hints and hints marked to avoid are left out.
        """
        hints = self.stored_data.get(self.hints_key) or []
        return {hint["location"]: hint["status"] for hint in hints
                if hint["status"] not in (HintStatus.HINT_FOUND, HintStatus.HINT_AVOID)
                and self.slot_concerns_self(hint["finding_player"])}

    def update(self) -> None:
        """Work out again which locations are in logic, then tell the listeners."""
        if not self.tracking:
            return
        self.core.set_missing_locations(self.missing_locations)
        # A seed from an older version of this world can send item codes with the retired field
        # set, which the current data package doesn't list. They name the same items as the
        # canonical codes do.
        self.core.set_items_received([item._replace(item=canonical_code(item.item))
                                      for item in self.items_received])
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
        """Return Soma's position as the game client stores it, [room, x, y].

        Returns None until the client has stored a valid position.
        """
        value = self.stored_data.get(self.position_key) if self.slot is not None else None
        try:
            room, x, y = value
        except (TypeError, ValueError):
            return None
        # Any client in the room can write this key, and the page reads the room id as text.
        if not (isinstance(room, str) and room and isinstance(x, int) and isinstance(y, int)):
            return None
        return value

    def follow_position(self) -> None:
        """Note where Soma came into his room, and plan the route again whenever he enters another.

        The new route then starts at the door he came in by.
        """
        position = self.position()
        if position is None:
            return
        if position[0] != self.entry_room:
            self.entry_room = position[0]
            try:
                self.entry_point = tracker_map.cell_position(*position)
            except KeyError:
                self.entry_point = None  # a room missing from this world's room data
        if self.route_target is not None and position[0] != self.route_room:
            self.plan_route()

    @property
    def race_mode(self) -> bool:
        """Whether the room is a race, where the tracker plans no routes."""
        return bool(self.stored_data.get(RACE_MODE_KEY))

    def set_route(self, location_id: int | None) -> None:
        """Show a route to the location with ``location_id``, or no route when it is None.

        A request for a location is ignored in a race room. It is also ignored when the slot or the
        tracker's rebuilt world doesn't have the location. The rebuilt world can lack one when the
        seed comes from another version of this world. An ignored request leaves the current route
        as it is.
        """
        if location_id is not None and (self.race_mode or not self.routable(location_id)):
            return
        self.route_target = location_id
        self.plan_route()
        self.changed()

    def routable(self, location_id: int) -> bool:
        """Whether the location is in both the slot and the world the tracker rebuilt."""
        world = self.core.get_current_world()
        if location_id not in self.server_locations or world is None:
            return False
        try:
            world.get_location(world.location_id_to_name.get(location_id))
        except KeyError:
            return False
        return True

    def plan_route(self) -> None:
        """Plan the route from Soma's room to the target location.

        The target is dropped in a race room, once its location is checked, and when the rebuilt
        world no longer has it. While the target can't be reached, the route is None but the
        target stays.
        """
        self.route = None
        if self.race_mode:
            self.route_target = None
            return
        if self.route_target in self.checked_locations:
            self.route_target = None  # checked, so there is nothing left to route to
        if self.route_target is None or not self.tracking or self.result is None:
            return
        if not self.routable(self.route_target):
            self.route_target = None  # the world has been rebuilt since, without this location
            return
        world = self.core.get_current_world()
        location = world.get_location(world.location_id_to_name[self.route_target])
        position = self.position()
        self.route_room = position[0] if position is not None else None
        # From the door Soma came in by, which he can get back to wherever he has gone since.
        start_point = self.entry_point if self.route_room == self.entry_room else None
        states = [state for state in (self.result.state, self.result.glitches_state)
                  if state is not None]
        self.route = tracker_route.plan_route(world, location, states, self.route_room,
                                              start_point)

    # --- what the page shows -------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Return everything the page shows, as JSON values."""
        snapshot = {
            "status": self.status,
            "connected": self.slot is not None,
            "following": self.following,  # including while it connects or waits to reconnect
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
        # Item smoothing holds some progression out of the pool to place it itself, such as the
        # Ancient Books and the mobility souls, so those come from get_pre_fill_items.
        in_pool = {item.name for item in (*world.multiworld.itempool, *world.get_pre_fill_items())
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
                # cells shown by the Castle Maps held, which the page draws like remembered ones
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
