"""
Routes for Universal Tracker's /get_logical_path command and its map page's Find Route button.

This world's regions are door nodes, so UT's own route reads as "Door: 000:003 -> 003:000", one
line per door. :func:`describe_route` lists the rooms on the way instead, one line per area, and
names what each room needs: a soul, a weapon to break a ceiling, or a trick.
"""
from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

from BaseClasses import CollectionState, Entrance, Region
from Utils import get_intended_text

from .regions import UT_TRICKS_ITEM
from .tracker_map import AREA_NAMES

if TYPE_CHECKING:
    from NetUtils import JSONMessagePart

    from . import CVAOSWorld

DOOR_NODE_PREFIX = "Entrance: "  # door nodes are named "Entrance: {room}:{door}"


def _room_of(region: Region | None) -> str | None:
    """Return the room a door node is in, or None for any other region."""
    if region is None or not region.name.startswith(DOOR_NODE_PREFIX):
        return None
    return region.name[len(DOOR_NODE_PREFIX):].split(":", 1)[0]


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


def describe_route(world: CVAOSWorld, dest_name: str,
                   state: CollectionState) -> list[list[JSONMessagePart]] | None:
    """Return a route to the location named ``dest_name`` as UT messages, one per line.

    ``dest_name`` can also be part of one location's name, such as "lucky charm". Returns None
    when no location has that name, or ``state`` can't reach it. UT then asks again with its state
    for tricks, and after that looks for a region or entrance of that name.
    """
    names = [location.name for location in world.get_locations()]
    target, usable, _ = get_intended_text(dest_name, names)
    if not usable:
        containing = [name for name in names if dest_name.lower() in name.lower()]
        if len(containing) != 1:
            return None
        target = containing[0]
    location = world.get_location(target)
    if not location.can_reach(state):
        return None

    messages: list[list[JSONMessagePart]] = []
    if state.has(UT_TRICKS_ITEM, world.player):
        messages.append([{"type": "color", "color": "yellow", "text": "Using tricks:"}])
    steps = route_steps(state, entrances_on_path(state, location.parent_region))
    for area, area_steps in itertools.groupby(steps, key=lambda step: AREA_NAMES[step[0][0]]):
        rooms = [f"{room} ({', '.join(needed)})" if needed else room for room, needed in area_steps]
        messages.append([{"type": "color", "color": "blue", "text": f"{area}:"},
                         {"type": "text", "text": " " + " > ".join(rooms)}])
    messages.append([{"type": "text", "text": "-> "},
                     {"type": "color", "color": "green", "text": location.name}])
    return messages
