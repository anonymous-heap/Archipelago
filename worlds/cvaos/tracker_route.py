"""
Routes to a location, for the trackers: Universal Tracker's /get_logical_path command and "Find
Route" button, and the map on the tracker page.

This world's regions are door nodes, so UT's own route reads as "Door: 000:003 -> 003:000", one
line per door. A route here lists the rooms on the way instead, one line per area, and names what
each room needs, such as a soul, a weapon to break a ceiling, or a trick.

UT's routes always begin at the start of the game, no matter where Soma is. :func:`plan_route` can
start in Soma's current room instead. :func:`route_points` returns the doors a route goes through
as points on the map, so the tracker can draw the route.

A defeat location can be checked in any room where its enemy appears. :func:`defeat_rooms` returns
the rooms where a state can reach the enemy, so the tracker page can show each room's pin with its
own status.
"""
from __future__ import annotations

import itertools
from collections import deque
from typing import TYPE_CHECKING, NamedTuple, Sequence

from BaseClasses import CollectionState, Entrance, Location, Region
from Utils import get_intended_text

from .data import area_names, map_info
from .data.entrance_info import rows as entrance_rows
from .data.room_info import by_identifier as rooms_by_identifier
from .regions import UT_TRICKS_ITEM

if TYPE_CHECKING:
    from NetUtils import JSONMessagePart

    from . import CVAOSWorld

DOOR_NODE_PREFIX = "Entrance: "  # door nodes are named "Entrance: {room}:{neighbor}"


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
    """Return the location named ``dest_name``, or the only location whose name contains it.

    Returns None when no single location matches.
    """
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

    ``state.path`` maps each region to a pair: its name, and the path that ends at the entrance it
    was reached through. An entrance's pair is likewise its name and the path that ends at its
    parent region.
    """
    names = []
    node = state.path.get(region)
    while node:
        name, node = node
        names.append(name)
    # The names alternate region and entrance, from region back to the start.
    return [region.multiworld.get_entrance(name, region.player) for name in reversed(names[1::2])]


def _search(state: CollectionState, starts: list[Region], goal: Region) -> list[Entrance] | None:
    """Return the fewest entrances that lead from any of ``starts`` to ``goal`` with ``state``.

    Returns None when there is no such path.
    """
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
    """Return the door node of ``room`` nearest ``point``, which is given in map cells.

    Returns None when none of the room's doors has a place on the map.
    """
    distances = []
    for name in world.entrance_region_names_by_room.get(room, []):
        door = DOOR_POINTS.get(name[len(DOOR_NODE_PREFIX):])
        if door is not None:
            distances.append(((door[0] - point[0]) ** 2 + (door[1] - point[1]) ** 2, name))
    return world.get_region(min(distances)[1]) if distances else None


def plan_route(world: CVAOSWorld, location: Location,
               states: CollectionState | Sequence[CollectionState], start_room: str | None = None,
               start_point: tuple[float, float] | None = None) -> Route | None:
    """Return a route to ``location`` through the fewest doors and rooms.

    ``states`` is one state, or states to try in turn, such as the state for logic and then the
    one with tricks. Returns None when none of them can reach the location.

    The route starts at the first of these places that leads to the location with any of the
    states:
      - the door of ``start_room`` nearest ``start_point``, the point in map cells where Soma came
        into the room;
      - any door of ``start_room``;
      - the start of the game.

    Soma's own door comes first because another door of the room may be out of his reach, such
    as one across a gap he can't cross yet.
    """
    if isinstance(states, CollectionState):
        states = [states]
    states = [state for state in states if location.can_reach(state)]
    starts: list[tuple[list[Region], str | None]] = []
    if start_room is not None:
        door = nearest_door(world, start_room, start_point) if start_point is not None else None
        if door is not None:
            starts.append(([door], start_room))
        names = world.entrance_region_names_by_room.get(start_room, [])
        starts.append(([world.get_region(name) for name in names], start_room))
    starts.append(([world.get_region(world.origin_region_name)], None))
    for regions, room in starts:
        for state in states:
            path = _search(state, regions, location.parent_region)
            if path is not None:
                return Route(location, path, room, state.has(UT_TRICKS_ITEM, world.player))
    return None


def defeat_rooms(location: Location, state: CollectionState) -> set[str]:
    """Return the rooms where ``state`` can reach the enemy whose defeat is ``location``.

    Each placement of the enemy connects to the location's region from doors of its own room
    (regions._create_enemy_checks). So the room of each such door that ``state`` can use is a room
    where the player can defeat the enemy.
    """
    return {_room_of(entrance.parent_region) for entrance in location.parent_region.entrances
            if entrance.can_reach(state)} - {None}


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
    for area, area_steps in itertools.groupby(steps, key=lambda step: area_names[step[0][0]]):
        rooms = [f"{room} ({', '.join(needed)})" if needed else room for room, needed in area_steps]
        messages.append([{"type": "color", "color": "blue", "text": f"{area}:"},
                         {"type": "text", "text": " " + " > ".join(rooms)}])
    messages.append([{"type": "text", "text": "-> "},
                     {"type": "color", "color": "green", "text": location.name}])
    return messages


def describe_route(world: CVAOSWorld, dest_name: str,
                   state: CollectionState) -> list[list[JSONMessagePart]] | None:
    """Return a route to the location named ``dest_name`` as UT messages, one per line.

    ``dest_name`` can also be part of a name, such as "lucky charm", when only one location's name
    contains it. Returns None when no location has that name, or ``state`` can't reach it. UT then
    asks again with its state for tricks, and after that looks for a region or entrance of that
    name.
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

    A door's data gives the cell just outside its room, as an offset from the room's position.
    The door's point is the middle of the edge between that cell and the room's cell beside it.
    A door is left out when no cell beside that one belongs to its room.
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
