"""
The tracker's connection to the Archipelago server, and what it knows about the slot it tracks.

It connects as a tracker (it takes no items and sends no checks), rebuilds this world from the
slot data with Universal Tracker's engine, and works out which locations are in logic whenever
items arrive or checks are made. The client keeps Soma's position and the explored map in data
storage, and the tracker follows both. :meth:`TrackerContext.snapshot` is everything the web page
shows, and listeners hear when it may have changed.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Callable

from CommonClient import CommonContext
from NetUtils import HintStatus
from Utils import async_start

from .. import CVAOSWorld, exploration, tracker_map, tracker_route
from ..data import item_info_collection
from ..regions import UT_TRICKS_ITEM
from .ut import CurrentTrackerState
from .ut.TrackerCore import TrackerCore

logger = logging.getLogger("Client")

LOG_LINES = 100  # how many of the client log's lines the page can show

# The page lists the progression items in these groups. Anything else is a key item.
ITEM_CATEGORIES = {info.name: info.item_category for info in item_info_collection}
ITEM_GROUPS = {"ability_soul": "Souls", "red_soul": "Souls", "blue_soul": "Souls",
               "yellow_soul": "Souls", "weapon": "Weapons"}


def same_address(first: str, second: str) -> bool:
    """Whether two server addresses name the same server, written with or without a scheme."""
    def bare(address: str) -> str:
        return address.strip().lower().removeprefix("wss://").removeprefix("ws://").rstrip("/")
    return bare(first) == bare(second)


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
        if password_requested and not self.password:
            self.set_status("This room needs a password")
            await self.disconnect()
            return
        if not self.slot_name:
            self.set_status("Enter a slot name to connect")
            await self.disconnect()
            return
        self.auth = self.slot_name
        await self.send_connect(game="")

    async def connect_to(self, address: str, slot_name: str, password: str | None) -> None:
        """Connect to the server at ``address`` and track ``slot_name``."""
        self.slot_name = slot_name
        self.username = slot_name  # CommonContext reconnects on its own only when this is set
        self.password = password or None
        self.set_status(f"Connecting to {address}")
        await self.connect(address)

    async def hand_over(self, address: str, slot_name: str, password: str | None) -> str:
        """Take a new launch's room and slot, unless this tracker is busy with another.

        Returns "opened" when the launch only needs to open this tracker's page, and "busy" when
        this tracker is connected to another room or tracks another slot. A tracker that isn't
        connected takes the launch's room and slot, which covers a room that came back on a new
        port after the old one closed.
        """
        if not address or not slot_name:
            return "opened"  # the launch asked for nothing but the page
        if self.slot is None:
            await self.connect_to(address, slot_name, password)
            return "opened"
        same_room = same_address(address, self.server_address or "")
        return "opened" if same_room and slot_name == self.slot_name else "busy"

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

    def set_route(self, location_id: int | None) -> None:
        """Show a route to the location with ``location_id``, or none."""
        self.route_target = location_id
        self.plan_route()
        self.changed()

    def plan_route(self) -> None:
        """Plan the route to the route's target from Soma's room, or drop it once it is checked."""
        self.route = None
        if self.route_target in self.checked_locations:
            self.route_target = None  # Soma got there
        if self.route_target is None or not self.tracking or self.result is None:
            return
        world = self.core.get_current_world()
        location = world.get_location(world.location_id_to_name[self.route_target])
        position = self.position()
        self.route_room = position[0] if position is not None else None
        for state in (self.result.state, self.result.glitches_state):
            if state is not None:
                self.route = tracker_route.plan_route(world, location, state, self.route_room)
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
