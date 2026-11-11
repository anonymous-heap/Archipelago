"""
Parsed Castlevania: Aria of Sorrow game data.

Each table's subpackage loads its CSVs once at import and exposes the result as an immutable
``rows`` tuple plus whatever lookup dicts it needs. This module re-exports each ``rows`` tuple
under a name that says which table it came from, such as ``item_info_collection``. The alias is
the same object, not a copy. It also re-exports the row types and a few lookups, such as
``enemy_type_by_name`` and the routing indexes.

Everything else is imported from its own subpackage. That covers the other lookup dicts, such as
``item_info.by_item_number``, and the worth and soul tier tables in ``item_balancing``. It also
covers ``map_info``, which holds the in-game map rather than a table.

Route solving over this data is in ``worlds/cvaos/tools/routing``, which may import from
here but is never imported by it.
"""

from __future__ import annotations

from .enemy_info import EnemyType, by_name as enemy_type_by_name, rows as enemy_type_collection
from .entrance_info import EntranceInfo, rows as entrance_info_collection
from .item_balancing import DesirabilityInfo, rows as desirability_collection
from .item_info import ItemInfo, rows as item_info_collection
from .pickup_info import PickupInfo, area_names, rows as pickup_info_collection
from .room_info import RoomInfo, rows as room_info_collection
from .routing_info import (
    AbilityCombo,
    EntranceToPickupRegionInfo,
    EntranceToEnemyRegionInfo,
    EnemyPlacement,
    RoutingInfo,
    TransdoorConnection,
    by_enemy_name_for_enemy_regions,
    by_enemy_number_for_enemy_regions,
    by_from_entrance_for_transdoor,
    enemy_meta_by_number,
    enemy_placements,
    enemy_region_rows as entrance_to_enemy_region_info_collection,
    lookup_pickup_region_requirement,
    pickup_region_rows as entrance_to_pickup_region_info_collection,
    rows as entrance_to_entrance_info_collection,
    transdoor_connection_rows as transdoor_connection_collection,
)

__all__ = [
    "AbilityCombo",
    "DesirabilityInfo",
    "EnemyPlacement",
    "EnemyType",
    "EntranceInfo",
    "EntranceToPickupRegionInfo",
    "EntranceToEnemyRegionInfo",
    "ItemInfo",
    "PickupInfo",
    "RoomInfo",
    "RoutingInfo",
    "TransdoorConnection",
    "area_names",
    "by_enemy_name_for_enemy_regions",
    "by_enemy_number_for_enemy_regions",
    "by_from_entrance_for_transdoor",
    "desirability_collection",
    "enemy_meta_by_number",
    "enemy_placements",
    "enemy_type_by_name",
    "enemy_type_collection",
    "entrance_info_collection",
    "entrance_to_entrance_info_collection",
    "entrance_to_enemy_region_info_collection",
    "entrance_to_pickup_region_info_collection",
    "item_info_collection",
    "lookup_pickup_region_requirement",
    "pickup_info_collection",
    "room_info_collection",
    "transdoor_connection_collection",
]
