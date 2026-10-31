"""
Routes to a location, for the trackers: Universal Tracker's /get_logical_path command and Find
Route button, and the Aria of Sorrow tracker's map.

This world's regions are door nodes, so UT's own route reads as "Door: 000:003 -> 003:000", one
line per door. A route here lists the rooms on the way instead, one line per area, and names what
each room needs: a soul, a weapon to break a ceiling, or a trick.

UT's routes start where the game does, since UT knows nothing of where Soma is. :func:`plan_route`
can start in the room he is in, and :func:`route_points` gives the doors it goes through as points
on the map, so the tracker can draw it.
"""
from __future__ import annotations

import itertools
from collections import deque
from typing import TYPE_CHECKING, NamedTuple

from BaseClasses import CollectionState, Entrance, Location, Region
from Utils import get_intended_text

from .data import map_info
from .data.entrance_info import rows as entrance_rows
from .data.room_info import by_identifier as rooms_by_identifier
from .regions import UT_TRICKS_ITEM
from .tracker_map import AREA_NAMES

if TYPE_CHECKING:
    from NetUtils import JSONMessagePart

    from . import CVAOSWorld

DOOR_NODE_PREFIX = "Entrance: "  # door nodes are named "Entrance: {room}:{door}"


class Route(NamedTuple):
    location: Location
    entrances: list[Entrance]  # in order, from the start of the route
    start_room: str | None     # the room the route starts in, or None for the start of the game
    tricks: bool               # whether the route needs a trick that logic leaves out


def _room_of(region: Region | None) -> str | None:
    """Return the room a door node is in, or None for any other region."""
    if region is None or not region.name.startswith(DOOR_NODE_PREFIX):
        return None
    return region.name[len(DOOR_NODE_PREFIX):].split(":", 1)[0]


def find_location(world: CVAOSWorld, dest_name: str) -> Location | None:
    """Return the location named ``dest_name``, or the one location whose name contains it."""
    names = [location.name for location in world.get_locations()]
    target, usable, _ = get_intended_text(dest_name, names)
    if not usable:
        containing = [name for name in names if dest_name.lower() in name.lower()]
        if len(containing) != 1:
            return None
        target = containing[0]
    return world.get_location(target)


def entrances_on_path(state: CollectionState, region: Region) -> list[Entrance]:
    """Return the entrances on ``state``'s path to ``region``, from the start of the game.

    ``state.path`` holds each region as its name and then the path to the entrance that reached
    it, and each entrance as its name and then the path to its own region.
    """
    names = []
    node = state.path.get(region)
    while node:
        name, node = node
        names.append(name)
    # The names alternate region and entrance, from region back to the start.
    return [region.multiworld.get_entrance(name, region.player) for name in reversed(names[1::2])]


def _search(state: CollectionState, starts: list[Region], goal: Region) -> list[Entrance] | None:
    """Return the fewest entrances that lead from any of ``starts`` to ``goal`` at ``state``."""
    reached_by: dict[Region, Entrance | None] = {start: None for start in starts}
    queue = deque(starts)
    while queue and goal not in reached_by:
        region = queue.popleft()
        for entrance in region.exits:
            target = entrance.connected_region
            if target is not None and target not in reached_by and entrance.access_rule(state):
                reached_by[target] = entrance
                queue.append(target)
    if goal not in reached_by:
        return None
    path = []
    while (entrance := reached_by[goal]) is not None:
        path.append(entrance)
        goal = entrance.parent_region
    return path[::-1]


def nearest_door(world: CVAOSWorld, room: str, point: tuple[float, float]) -> Region | None:
    """Return the door node of ``room`` nearest ``point``, in map cells.

    Returns None when none of the room's doors has a place on the map.
    """
    distances = []
    for name in world.entrance_region_names_by_room.get(room, []):
        door = DOOR_POINTS.get(name[len(DOOR_NODE_PREFIX):])
        if door is not None:
            distances.append(((door[0] - point[0]) ** 2 + (door[1] - point[1]) ** 2, name))
    return world.get_region(min(distances)[1]) if distances else None


def plan_route(world: CVAOSWorld, location: Location, state: CollectionState,
               start_room: str | None = None,
               start_point: tuple[float, float] | None = None) -> Route | None:
    """Return the route at ``state`` to ``location`` through the fewest doors and rooms.

    The route starts in ``start_room`` when one is given and the location can be reached from
    there, and at the start of the game otherwise. Returns None when ``state`` can't reach it.

    Within the room, the route starts at the door nearest ``start_point``, Soma's position in map
    cells, when that is given. Another door of the room may be one he can't get to from where he
    is, such as across a gap he can't yet cross. Only when no route leads from the nearest door
    does it start from any door of the room.
    """
    if not location.can_reach(state):
        return None
    path = None
    if start_room is not None:
        door = nearest_door(world, start_room, start_point) if start_point is not None else None
        if door is not None:
            path = _search(state, [door], location.parent_region)
        if path is None:
            names = world.entrance_region_names_by_room.get(start_room, [])
            path = _search(state, [world.get_region(name) for name in names],
                           location.parent_region)
    if path is None:
        start_room = None
        path = _search(state, [world.get_region(world.origin_region_name)], location.parent_region)
    if path is None:
        return None
    return Route(location, path, start_room, state.has(UT_TRICKS_ITEM, world.player))


def route_steps(state: CollectionState, entrances: list[Entrance]) -> list[tuple[str, list[str]]]:
    """Return the rooms that ``entrances`` lead through, each with what the route needs there."""
    steps: list[tuple[str, list[str]]] = []
    for entrance in entrances:
        room = _room_of(entrance.parent_region)
        if room is not None and (not steps or steps[-1][0] != room):
            steps.append((room, []))
        uses = getattr(entrance.access_rule, "uses", None)
        if steps and uses is not None:
            needed = steps[-1][1]
            needed.extend(thing for thing in uses(state) if thing not in needed)
        next_room = _room_of(entrance.connected_region)
        if next_room is not None and (not steps or steps[-1][0] != next_room):
            steps.append((next_room, []))
    return steps


def route_messages(location: Location, steps: list[tuple[str, list[str]]],
                   tricks: bool) -> list[list[JSONMessagePart]]:
    """Return a route through ``steps`` to ``location`` as UT messages, one per line."""
    messages: list[list[JSONMessagePart]] = []
    if tricks:
        messages.append([{"type": "color", "color": "yellow", "text": "Using tricks:"}])
    for area, area_steps in itertools.groupby(steps, key=lambda step: AREA_NAMES[step[0][0]]):
        rooms = [f"{room} ({', '.join(needed)})" if needed else room for room, needed in area_steps]
        messages.append([{"type": "color", "color": "blue", "text": f"{area}:"},
                         {"type": "text", "text": " " + " > ".join(rooms)}])
    messages.append([{"type": "text", "text": "-> "},
                     {"type": "color", "color": "green", "text": location.name}])
    return messages


def describe_route(world: CVAOSWorld, dest_name: str,
                   state: CollectionState) -> list[list[JSONMessagePart]] | None:
    """Return a route to the location named ``dest_name`` as UT messages, one per line.

    ``dest_name`` can also be part of one location's name, such as "lucky charm". Returns None
    when no location has that name, or ``state`` can't reach it. UT then asks again with its state
    for tricks, and after that looks for a region or entrance of that name.
    """
    location = find_location(world, dest_name)
    if location is None or not location.can_reach(state):
        return None
    steps = route_steps(state, entrances_on_path(state, location.parent_region))
    return route_messages(location, steps, state.has(UT_TRICKS_ITEM, world.player))


def _signed_byte(value: int) -> int:
    value &= 0xFF
    return value - 0x100 if value & 0x80 else value


def _door_points() -> dict[str, tuple[float, float]]:
    """Return where each door is on the map, in map cells, by its door node's identifier.

    A door's position is the cell just outside its room, relative to the room's own cell. The
    point is the middle of the edge between that cell and the room's cell beside it.
    """
    points = {}
    for door in entrance_rows:
        room = rooms_by_identifier[door.room_identifier]
        outside_x = int(room.x) + _signed_byte(door.x_pos_door)
        outside_y = int(room.y) + _signed_byte(door.y_pos_door)
        for step_x, step_y in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            if map_info.room_at(outside_x + step_x, outside_y + step_y) == door.room_identifier:
                points[door.door_identifier_unique] = (outside_x + 0.5 + step_x / 2,
                                                       outside_y + 0.5 + step_y / 2)
                break
    return points


DOOR_POINTS = _door_points()


def route_points(route: Route) -> list[tuple[float, float]]:
    """Return the doors ``route`` goes through, in order, as points on the map in map cells.

    A door the map has no place for, such as one into a room it draws elsewhere, is left out.
    """
    points = []
    for entrance in route.entrances:
        room, next_room = _room_of(entrance.parent_region), _room_of(entrance.connected_region)
        if room is not None and next_room is not None and room != next_room:
            door = entrance.parent_region.name[len(DOOR_NODE_PREFIX):]
            if door in DOOR_POINTS:
                points.append(DOOR_POINTS[door])
    return points
