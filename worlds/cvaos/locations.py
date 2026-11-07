"""
Location tables and location groups for Castlevania: Aria of Sorrow.

The tables name a location for every ground pickup, and for the first defeat of each kind of
enemy that enemy checks can cover. They hold all of these whatever the options, so location ids
stay the same in the data package. This module also writes the hint text of a defeat location.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from BaseClasses import Location

from .data import area_names, enemy_placements, pickup_info_collection
from .enemy_checks import (ARENA_PLACEMENTS, BOSS_TYPES, SPAWNS_AFTER_BOSS_IN, checkable_types,
                           defeat_location_id, defeat_location_name, has_recorded_route,
                           placements_of)

if TYPE_CHECKING:
    from . import CVAOSWorld

__all__ = [
    "CVAOSLocation",
    "location_name_to_id",
    "location_id_to_name",
    "flag_offset_to_location_id",
    "pickup_location_ids",
    "defeat_location_names",
    "location_name_groups",
    "defeat_hint_text",
]


class CVAOSLocation(Location):
    """
    A location in Castlevania: Aria of Sorrow.

    It is either a ground pickup or, with enemy checks on, the first defeat of a kind of enemy
    (see enemy_checks.py).
    """
    game: str = "Castlevania - Aria of Sorrow"


def _build_location_tables() -> tuple[dict[str, int], dict[int, str]]:
    """
    Build the pickup locations' name-to-id and id-to-name tables from pickup_info_collection.

    A location's name is its PickupInfo.location_name: the room area's friendly name plus a
    bracketed "<room> <item>" tag, such as "White Dragon Hallway [00D Potion (A)]". Its id is the
    pickup's ptr_address. A duplicate name or id raises ValueError.
    """
    name_to_id: dict[str, int] = {}
    id_to_name: dict[int, str] = {}

    for pickup in pickup_info_collection:
        location_name = pickup.location_name
        # Use ptr_address as the unique location ID
        location_id = pickup.ptr_address

        if location_name in name_to_id:
            raise ValueError(f"Duplicate location name: {location_name}")
        if location_id in id_to_name:
            raise ValueError(f"Duplicate location ID: {location_id:#x}")

        name_to_id[location_name] = location_id
        id_to_name[location_id] = location_name

    return name_to_id, id_to_name


location_name_to_id, location_id_to_name = _build_location_tables()

# The pickup locations, whose ids are ROM pointers. Only these are written into the ROM, and only
# these count for item smoothing.
pickup_location_ids: frozenset[int] = frozenset(location_id_to_name)

# Every possible defeat location joins the tables, whatever the options, so their ids are stable in
# the data package. regions.create_regions creates the ones the options ask for.
defeat_location_names: frozenset[str] = frozenset(
    defeat_location_name(enemy.name) for enemy in checkable_types())
for _enemy in checkable_types():
    _name, _location_id = defeat_location_name(_enemy.name), defeat_location_id(_enemy.id)
    if _name in location_name_to_id or _location_id in location_id_to_name:
        raise ValueError(f"defeat location {_name!r} ({_location_id:#x}) clashes with a pickup")
    location_name_to_id[_name] = _location_id
    location_id_to_name[_location_id] = _name


def _build_location_name_groups() -> dict[str, set[str]]:
    """
    Return the location groups, which let a YAML exclude or prioritize a set of enemy checks.

    "Enemy Defeats" holds every defeat location, and "Boss Defeats" holds those of BOSS_TYPES.
    "Unrouted Enemy Defeats" holds the checks that need enemy_checks.FALLBACK_SOULS. Each
    "<Area> Defeats" group holds the checks of the types that appear in that area, so a type joins
    the group of every area where it appears.

    The groups hold every possible defeat location, whatever the options. Archipelago skips a
    grouped location that a seed doesn't create.
    """
    groups: dict[str, set[str]] = {"Enemy Defeats": set(), "Boss Defeats": set(),
                                   "Unrouted Enemy Defeats": set()}
    for enemy in checkable_types():
        name = defeat_location_name(enemy.name)
        groups["Enemy Defeats"].add(name)
        if enemy.name in BOSS_TYPES:
            groups["Boss Defeats"].add(name)
        if not has_recorded_route(enemy.name):
            groups["Unrouted Enemy Defeats"].add(name)
        for placement in placements_of(enemy.name):
            groups.setdefault(f"{area_names[placement.room[0]]} Defeats", set()).add(name)
    return groups


location_name_groups: dict[str, set[str]] = _build_location_name_groups()

# The boss in each arena room, such as Legion in room 725.
_ARENA_BOSS: dict[str, str] = {placement.room: placement.name for placement in enemy_placements
                               if placement.enemy_number in ARENA_PLACEMENTS}


def defeat_hint_text(enemy_name: str) -> str:
    """
    Return the rooms where ``enemy_name`` appears, as a hint for its defeat location shows them.

    The rooms are grouped by area. Both the areas and the rooms within each area come in
    identifier order. A room is marked with a boss when the enemy appears there only after that
    boss is beaten in another room. For Alura Une, this returns "Underground Reservoir 710 (after
    Legion); Forbidden Area A03".
    """
    bosses_by_room: dict[str, set[str | None]] = {}
    for placement in placements_of(enemy_name):
        boss_room = SPAWNS_AFTER_BOSS_IN.get(placement.enemy_number)
        boss = _ARENA_BOSS[boss_room] if boss_room is not None else None
        bosses_by_room.setdefault(placement.room, set()).add(boss)
    rooms_by_area: dict[str, list[str]] = {}
    for room in sorted(bosses_by_room):
        bosses = bosses_by_room[room]
        text = room if None in bosses else f"{room} (after {' or '.join(sorted(bosses))})"
        rooms_by_area.setdefault(area_names[room[0]], []).append(text)
    return "; ".join(f"{area} {', '.join(rooms)}" for area, rooms in rooms_by_area.items())


def _build_flag_offset_map() -> dict[int, int]:
    """
    Map each pickup's save-flag bit to its location id.

    The key is the pickup's ``flag_offset``, its bit index in the collected-pickup bitfield, and
    the value is its ``ptr_address``. The client reads that bitfield and turns each set bit back
    into a location check. No two pickups share a flag offset, and a duplicate raises ValueError.
    """
    flag_to_id: dict[int, int] = {}
    for pickup in pickup_info_collection:
        if pickup.flag_offset <= 0:
            continue  # no tracked save flag
        if pickup.flag_offset in flag_to_id:
            raise ValueError(
                f"Duplicate pickup flag_offset {pickup.flag_offset} "
                f"({flag_to_id[pickup.flag_offset]:#x} vs {pickup.ptr_address:#x})")
        flag_to_id[pickup.flag_offset] = pickup.ptr_address
    return flag_to_id


flag_offset_to_location_id = _build_flag_offset_map()
