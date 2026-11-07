"""
Seed depth: how far into one cvaos player's seed a location, a room or an enemy comes.

Seed depth runs from 0 to 1. A thing's seed depth is the share of this world's pickup locations
that the seed's sphere order reaches before it. The order is the one ``item_smoothing`` uses for
the run: by multiworld sphere, then by map distance from the start. Within one sphere, a pickup
or room at the same map distance as another counts as reached at the same time, so neither is
before the other.

* A pickup location's depth counts the pickups that come strictly before it.
* A room's place in the order is the sphere where logic first reaches it, then the map distance
  of its nearest door.
* An enemy's depth is that of the first room where it appears, leaving out Boss Rush. A defeat
  location takes its enemy's depth, so it counts as reached once one of its enemy's rooms is.
* Anything the sweep never reaches is at depth 1.0. Under minimal accessibility, that includes
  every location and enemy that logic can't reach.

The depths describe the final placement, so read them only once core's progression balancing has
run, which is after every world's ``stage_post_fill``. No stage hook runs after balancing on the
main thread: ``stage_generate_output`` runs in the same thread pool as each player's
``generate_output`` and ``fill_slot_data``, all at once. So ``seed_depths`` sweeps each world the
first time any of them asks, under a lock of that world's own, and keeps the result for later
calls.
"""
from __future__ import annotations

import threading
import weakref
from bisect import bisect_left
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, Tuple

from BaseClasses import CollectionState

from .data import enemy_type_collection
from .enemy_checks import enemy_id_for_location, placements_of
from .item_smoothing import UNREACHED_DEPTH, region_depth
from .locations import pickup_location_ids
from .regions import can_reach_room

if TYPE_CHECKING:
    from . import CVAOSWorld

__all__ = ["SeedDepths", "enemy_seed_depths", "seed_depths"]

UNREACHED = 1.0

# Each world's lock and its swept depths. _locks_guard guards both dicts, and each world's own
# lock keeps a second thread from sweeping that world while the first one does.
_locks_guard = threading.Lock()
_locks: "weakref.WeakKeyDictionary[CVAOSWorld, threading.Lock]" = weakref.WeakKeyDictionary()
_swept: "weakref.WeakKeyDictionary[CVAOSWorld, SeedDepths]" = weakref.WeakKeyDictionary()


@dataclass(frozen=True)
class SeedDepths:
    """One player's seed depths, each from 0 to 1."""

    locations: Dict[str, float]  # each pickup and defeat location of the player, by name
    rooms: Dict[str, float]      # each room where an enemy appears, by room id
    enemies: Dict[int, float]    # each enemy type, by enemy id


def seed_depths(world: "CVAOSWorld") -> SeedDepths:
    """Return ``world``'s seed depths, sweeping its seed's spheres on the first call.

    Safe to call from several output threads at once. Call it only after progression balancing.
    """
    with _locks_guard:
        lock = _locks.setdefault(world, threading.Lock())
    with lock:
        with _locks_guard:
            depths = _swept.get(world)
        if depths is None:
            depths = _sweep(world)
            with _locks_guard:
                _swept[world] = depths
        return depths


def enemy_seed_depths(world: "CVAOSWorld") -> Dict[int, float]:
    """Return the seed depth of every enemy type in ``world``'s seed, by enemy id."""
    return seed_depths(world).enemies


def _sweep(world: "CVAOSWorld") -> SeedDepths:
    """Sweep the multiworld's spheres once and return ``world``'s seed depths.

    Each sphere's rooms are those that logic reaches with the items of the earlier spheres, which
    is also when that sphere's locations become reachable.
    """
    multiworld, player = world.multiworld, world.player
    map_depth = region_depth(multiworld, player)
    rooms = sorted({placement.room for enemy in enemy_type_collection
                    for placement in placements_of(enemy.name)})

    state = CollectionState(multiworld)
    locations = set(multiworld.get_filled_locations())
    pickup_keys: Dict[str, Tuple[int, int]] = {}
    room_keys: Dict[str, Tuple[int, int]] = {}
    sphere_index = 0
    while True:
        for room in rooms:
            if room not in room_keys and can_reach_room(state, world, room):
                doors = world.entrance_region_names_by_room.get(room, ())
                room_keys[room] = (sphere_index, min(map_depth.get(name, UNREACHED_DEPTH)
                                                     for name in doors))
        sphere = {location for location in locations if location.can_reach(state)}
        if not sphere:
            break
        for location in sphere:
            if location.player == player and location.address in pickup_location_ids:
                pickup_keys[location.name] = (
                    sphere_index, map_depth.get(location.parent_region.name, UNREACHED_DEPTH))
        for location in sphere:
            state.collect(location.item, True, location)
        locations -= sphere
        sphere_index += 1

    ordered = sorted(pickup_keys.values())
    count = max(1, len(ordered))

    def depth_of(key: Tuple[int, int]) -> float:
        return bisect_left(ordered, key) / count

    room_depths = {room: depth_of(key) for room, key in room_keys.items()}
    enemies = {enemy.id: min((room_depths[placement.room] for placement in placements_of(enemy.name)
                              if placement.room in room_depths), default=UNREACHED)
               for enemy in enemy_type_collection}
    location_depths: Dict[str, float] = {}
    for location in multiworld.get_locations(player):
        if location.address in pickup_location_ids:
            key = pickup_keys.get(location.name)
            location_depths[location.name] = UNREACHED if key is None else depth_of(key)
        elif location.address is not None:
            enemy_id = enemy_id_for_location(location.address)
            if enemy_id is not None:
                location_depths[location.name] = enemies[enemy_id]
    return SeedDepths(location_depths, room_depths, enemies)
