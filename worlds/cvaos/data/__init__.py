"""
Parsed Castlevania: Aria of Sorrow game data.

Each subpackage loads its CSVs once at import and exposes the result as an immutable
``rows`` tuple plus whatever lookup dicts it needs. This module is the single import
point for the rest of the world, so it re-exports each ``rows`` tuple under a name that
says which table it came from. The alias is the same object, not a copy.

Route solving over this data lives in ``worlds/cvaos/tools/routing``, which may import
from here but is never imported by it.
"""

from __future__ import annotations

from .entrance_info import EntranceInfo, rows as entrance_info_collection
from .item_balancing import DesirabilityInfo, rows as desirability_collection
from .item_info import ItemInfo, rows as item_info_collection
from .pickup_info import PickupInfo, rows as pickup_info_collection
from .room_info import RoomInfo, rows as room_info_collection
from .routing_info import (
    AbilityCombo,
    EntranceToPickupRegionInfo,
    EntranceToEnemyRegionInfo,
    RoutingInfo,
    TransdoorConnection,
    by_enemy_name_for_enemy_regions,
    by_enemy_number_for_enemy_regions,
    by_from_entrance_for_transdoor,
    enemy_meta_by_number,
    enemy_region_rows as entrance_to_enemy_region_info_collection,
    lookup_pickup_region_requirement,
    pickup_region_rows as entrance_to_pickup_region_info_collection,
    resolve_enemy_number,
    rows as entrance_to_entrance_info_collection,
    transdoor_connection_rows as transdoor_connection_collection,
)

__all__ = [
    "AbilityCombo",
    "DesirabilityInfo",
    "EntranceInfo",
    "EntranceToPickupRegionInfo",
    "EntranceToEnemyRegionInfo",
    "ItemInfo",
    "PickupInfo",
    "RoomInfo",
    "RoutingInfo",
    "TransdoorConnection",
    "by_enemy_name_for_enemy_regions",
    "by_enemy_number_for_enemy_regions",
    "by_from_entrance_for_transdoor",
    "desirability_collection",
    "enemy_meta_by_number",
    "entrance_info_collection",
    "entrance_to_entrance_info_collection",
    "entrance_to_enemy_region_info_collection",
    "entrance_to_pickup_region_info_collection",
    "item_info_collection",
    "lookup_pickup_region_requirement",
    "pickup_info_collection",
    "resolve_enemy_number",
    "room_info_collection",
    "transdoor_connection_collection",
]
