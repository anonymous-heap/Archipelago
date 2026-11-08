"""
The entrance graph extended with pickup and enemy nodes.

A pickup node is written "PICKUP:N", where N is the pickup number, and the graph has one for every
pickup. An enemy node is written "ENEMY:N", where N is the enemy number. The graph has one for
each placement that ``regions.py`` gives an enemy region: a placement of ``LOGIC_ENEMY_TYPES``
that respawns, so its soul can be farmed.

Each routing row for a pickup or an enemy becomes one edge, from the door that the row names to
the pickup or enemy node, with the row's requirement masks. As in ``regions.py``, the edge leads
only towards the pickup or enemy. The CSVs call these rows symmetric, but an edge back to the door
would let a search walk from one door of a room to another through a pickup, which generation
does not allow.

The searches in :class:`~.entrances.RoutingQueries` work on the extended graph as well.
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Set, Tuple

from ...data import (
    EntranceToEnemyRegionInfo,
    EntranceToPickupRegionInfo,
    entrance_info_collection,
    entrance_to_enemy_region_info_collection,
    entrance_to_pickup_region_info_collection,
    enemy_meta_by_number,
    pickup_info_collection,
)
from ...regions import LOGIC_ENEMY_TYPES, ONE_TIME_ENEMY_NUMBERS
from .entrances import (
    Edge,
    RouteOption,
    RoutingGraph,
    RoutingGraphBuilder,
    RoutingQueries,
)

NodeId = str
ReqMask = int


class PickupNodeId:
    """
    Helpers for representing pickup regions as graph nodes.
    """

    _PREFIX = "PICKUP:"

    @staticmethod
    def make(pickup_number: int) -> NodeId:
        return f"{PickupNodeId._PREFIX}{int(pickup_number)}"

    @staticmethod
    def is_pickup(node_id: NodeId) -> bool:
        return node_id.startswith(PickupNodeId._PREFIX)

    @staticmethod
    def number(node_id: NodeId) -> int:
        return int(node_id[len(PickupNodeId._PREFIX) :])


class EnemyNodeId:
    """
    Helpers for representing enemy regions as graph nodes.

    ``data.enemy_meta_by_number`` gives the enemy type, specifier and room for a node's number.
    """

    _PREFIX = "ENEMY:"

    @staticmethod
    def make(enemy_number: int) -> NodeId:
        return f"{EnemyNodeId._PREFIX}{int(enemy_number)}"

    @staticmethod
    def is_enemy(node_id: NodeId) -> bool:
        return node_id.startswith(EnemyNodeId._PREFIX)

    @staticmethod
    def number(node_id: NodeId) -> int:
        return int(node_id[len(EnemyNodeId._PREFIX) :])


def logic_enemy_numbers() -> Tuple[int, ...]:
    """Return the enemy numbers that get an enemy node, which are those with an enemy region."""
    return tuple(
        number for number, (enemy_name, _, _) in enemy_meta_by_number.items()
        if enemy_name in LOGIC_ENEMY_TYPES and number not in ONE_TIME_ENEMY_NUMBERS
    )


def add_pickups_and_enemies(
    graph: RoutingGraph,
    pickup_rows: Iterable[EntranceToPickupRegionInfo] = entrance_to_pickup_region_info_collection,
    enemy_rows: Iterable[EntranceToEnemyRegionInfo] = entrance_to_enemy_region_info_collection,
) -> RoutingGraph:
    """
    Return ``graph`` with the pickup and enemy nodes and their edges added.

    A row is skipped when its door is not in ``entrance_info``, or when its pickup or enemy has
    no node, which is what ``regions.py`` does. A row with no requirement mask has no
    requirement.
    """
    doors = {door.door_identifier_unique for door in entrance_info_collection}
    pickup_nodes = {pickup.pickup_number: PickupNodeId.make(pickup.pickup_number)
                    for pickup in pickup_info_collection}
    enemy_nodes = {number: EnemyNodeId.make(number) for number in logic_enemy_numbers()}

    edges: List[Tuple[NodeId, Edge]] = []
    rows = [(row, pickup_nodes.get(row.pickup_number)) for row in pickup_rows]
    rows += [(row, enemy_nodes.get(row.enemy_number)) for row in enemy_rows]
    for row, target in rows:
        if target is None or row.entrance_identifier not in doors:
            continue
        masks = tuple(int(mask) for mask in row.get_requirement_bitmasks()) or (0,)
        edges.append((row.entrance_identifier, Edge(
            to_node=target,
            connection_number=None,
            req_masks=masks,
            variant=getattr(row, "variant", None),
        )))

    return graph.with_edges([*pickup_nodes.values(), *enemy_nodes.values()], edges)


def default_graph(*, open_gates: bool = False) -> RoutingGraph:
    """
    Return the extended graph built from the bundled routing data.

    ``open_gates`` is passed to :meth:`RoutingGraphBuilder.from_requirements`.
    """
    return add_pickups_and_enemies(RoutingGraphBuilder.from_requirements(open_gates=open_gates))


def reachable_pickup_numbers_from_entrance(
    graph: RoutingGraph, start_entrance: NodeId, have_mask: ReqMask
) -> Set[int]:
    """
    Return the numbers of the pickups reachable from ``start_entrance`` under ``have_mask``.
    """
    reachable_nodes = RoutingQueries.reachable_entrance_nodes(graph, start_entrance, have_mask)
    return {
        PickupNodeId.number(node_id)
        for node_id in reachable_nodes
        if PickupNodeId.is_pickup(node_id)
    }


def pickup_route_options(
    graph: RoutingGraph,
    start_entrance: NodeId,
    pickup_number: int,
    *,
    have_mask: Optional[ReqMask] = None,
    limit: Optional[int] = None,
) -> Tuple[RouteOption, ...]:
    """
    Return :meth:`RoutingQueries.route_options` for reaching the pickup ``pickup_number``.
    """
    return RoutingQueries.route_options(
        graph,
        start_entrance,
        PickupNodeId.make(pickup_number),
        have_mask=have_mask,
        limit=limit,
    )
