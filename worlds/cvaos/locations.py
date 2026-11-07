"""
Location tables and location groups for Castlevania: Aria of Sorrow.

The tables name a location for every ground pickup, and for the first defeat of each kind of
enemy that enemy checks can cover. They hold all of these whatever the options, so location ids
stay the same in the data package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from BaseClasses import Location

from .data import pickup_info_collection
from .enemy_checks import (BOSS_TYPES, checkable_types, defeat_location_id, defeat_location_name,
                           has_recorded_route, placements_of)
from .tracker_map import AREA_NAMES

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
            groups.setdefault(f"{AREA_NAMES[placement.room[0]]} Defeats", set()).add(name)
    return groups


location_name_groups: dict[str, set[str]] = _build_location_name_groups()


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
