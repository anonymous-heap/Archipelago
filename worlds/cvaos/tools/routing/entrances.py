"""
The entrance-node graph and the searches over it.

:class:`RoutingGraphBuilder` builds the graph from the same data, with the same conventions, as
``regions.create_regions`` builds generation's regions, so each node here is a region there. An
entrance node is written "ROOM:NEIGHBOR" and stands for the door in ROOM that leads to NEIGHBOR.
Where two doors of a room lead to the same neighbor, the node also carries the door's position
tag, as in "103:104 (left)". The graph has a node for every door in ``entrance_info``, plus each
portal node that only the transdoor table names.

The graph has three kinds of edge:

* Each crossing in ``transdoor_connection_collection`` is an edge with no requirement. It leads
  from the door on one side of a doorway to the door on the other side.
* Each row of ``entrance_to_entrance_requirements.csv`` is an edge inside room ``RoomID``, from
  "RoomID:From" to "RoomID:To", with the row's requirement masks. A row with no mask has no
  requirement.
* Each route in ``regions.SPECIAL_ROOM_ROUTING`` is an edge between a portal node and a door.

``regions.py`` also writes a few rules in code, and no mask can express them:

* The true-ending gate on the 507 -> 506 and 904 -> 900 crossings requires the Ancient Books and
  enemy souls. The graph leaves both crossings out unless ``open_gates`` is set.
* With ``forbidden_area_button`` set to pickup, the A01 routes between 20D and A02 require the
  Study Sealswitch. The graph keeps the CSV rule, which is what generation uses with the option
  off.

The searches compare masks bit by bit and do not resolve them against options. A technique bit
such as Enemy is satisfied only when the searched ``have_mask`` holds it. The same goes for the
Tform bit, although generation satisfies Tform by reaching a Devil or Manticore. Generation never
satisfies Kick or Chronomage, because neither soul is an item.

:class:`RoutingQueries` searches the graph. It finds the nodes reachable with a given set of
abilities, and the subset-minimal sets of abilities for reaching each node.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import heapq
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from ...data import (
    AbilityCombo,
    RoutingInfo,
    TransdoorConnection,
    entrance_info_collection,
    entrance_to_entrance_info_collection,
    transdoor_connection_collection,
)
from ...regions import SPECIAL_ROOM_ROUTING, SPECIAL_TRANSDOOR_RULES

NodeId = str
ReqMask = int


# ============================================================
# Graph and result types
# ============================================================

@dataclass(frozen=True, slots=True)
class Edge:
    """
    A directed edge with one or more alternative requirement masks.

    The edge leads to ``to_node``. It is traversable when the player has every ability in at
    least one of the masks in ``req_masks``, and the mask ``0`` needs nothing.

    ``connection_number`` and ``variant`` identify the routing row that the edge comes from. Both
    are None for an edge that no routing row describes, such as a door crossing.
    """
    to_node: NodeId
    connection_number: Optional[int]
    req_masks: Tuple[ReqMask, ...]
    variant: Optional[int] = None


@dataclass(frozen=True, slots=True)
class BFSStep:
    from_node: NodeId
    to_node: NodeId
    connection_number: Optional[int]
    variant: Optional[int]
    usable_req_masks: Tuple[ReqMask, ...]


@dataclass(frozen=True, slots=True)
class BFSResult:
    reachable: bool
    path_nodes: Tuple[NodeId, ...]
    steps: Tuple[BFSStep, ...]


@dataclass(frozen=True, slots=True)
class ParentInfo:
    prev_node: NodeId
    prev_mask: ReqMask
    edge_from: NodeId
    edge_to: NodeId
    connection_number: Optional[int]
    variant: Optional[int]
    chosen_edge_req_mask: ReqMask


@dataclass(frozen=True, slots=True)
class MinReqResult:
    best: Mapping[NodeId, Set[ReqMask]]
    parent: Mapping[Tuple[NodeId, ReqMask], ParentInfo]


@dataclass(frozen=True, slots=True)
class RouteOption:
    """
    One way to reach a goal: a requirement mask, and an example path that needs only that mask.

    ``required_mask`` is one of the subset-minimal masks for the goal. ``steps`` lists the edges
    of one path from the start to the goal, and the mask options chosen on those edges together
    require exactly ``required_mask``.
    """
    required_mask: ReqMask
    steps: Tuple[ParentInfo, ...]


# ============================================================
# Entrance ids and requirement masks
# ============================================================

class EntranceId:
    """
    Helpers for entrance node ids.

    An entrance node id such as "000:003" names the door in room 000 that leads to room 003.
    Soma starts at "000:003".
    """

    _SEP = ":"

    @staticmethod
    def make(start_room: str, end_room: str) -> NodeId:
        return f"{start_room}{EntranceId._SEP}{end_room}"


class MaskUtils:
    @staticmethod
    def from_flags(*flags: AbilityCombo) -> ReqMask:
        mask = 0
        for flag in flags:
            mask |= int(flag)
        return mask

    @staticmethod
    def satisfied(have_mask: ReqMask, req_mask: ReqMask) -> bool:
        return (have_mask & req_mask) == req_mask

    @staticmethod
    def usable_options(have_mask: ReqMask, req_masks: Sequence[ReqMask]) -> Tuple[ReqMask, ...]:
        return tuple(req_mask for req_mask in req_masks if MaskUtils.satisfied(have_mask, req_mask))

    @staticmethod
    def edge_traversable(have_mask: ReqMask, req_masks: Sequence[ReqMask]) -> bool:
        return any(MaskUtils.satisfied(have_mask, req_mask) for req_mask in req_masks)

    @staticmethod
    def decode(mask: ReqMask) -> Tuple[str, ...]:
        if mask == 0:
            return ("None",)

        names: List[str] = []
        for flag in AbilityCombo:
            if flag == AbilityCombo.None_:
                continue
            if mask & int(flag):
                names.append(flag.name)

        # Show any bits that no flag defines, so a bad mask is visible when debugging.
        known_bits = 0
        for flag in AbilityCombo:
            known_bits |= int(flag)
        extra_bits = mask & ~known_bits
        if extra_bits:
            names.append(hex(extra_bits))

        return tuple(names)

    @staticmethod
    def format(mask: ReqMask) -> str:
        return "|".join(MaskUtils.decode(mask))


# ============================================================
# Routing graph and builder
# ============================================================

FromToKey = Tuple[NodeId, NodeId]


@dataclass(frozen=True, slots=True)
class RoutingGraph:
    """
    A directed graph whose edges carry requirement masks, stored as an adjacency list.

    ``adj`` maps every node to its outgoing edges, so a node without one maps to an empty list.
    ``by_from_to`` lists the edges between each ordered pair of nodes.
    """
    adj: Mapping[NodeId, Sequence[Edge]]
    by_from_to: Mapping[FromToKey, Tuple[Edge, ...]]

    @staticmethod
    def from_edges(nodes: Iterable[NodeId], edges: Iterable[Tuple[NodeId, Edge]]) -> RoutingGraph:
        """
        Return a graph with the given nodes and edges.

        Each edge is a pair of its source node and the :class:`Edge` itself. The endpoints of an
        edge become nodes even when ``nodes`` leaves them out.
        """
        adjacency: Dict[NodeId, List[Edge]] = {node: [] for node in nodes}
        by_from_to: Dict[FromToKey, List[Edge]] = {}
        for from_node, edge in edges:
            adjacency.setdefault(from_node, []).append(edge)
            adjacency.setdefault(edge.to_node, [])
            by_from_to.setdefault((from_node, edge.to_node), []).append(edge)
        return RoutingGraph(
            adj=adjacency,
            by_from_to={key: tuple(found) for key, found in by_from_to.items()},
        )

    def with_edges(
        self, nodes: Iterable[NodeId], edges: Iterable[Tuple[NodeId, Edge]]
    ) -> RoutingGraph:
        """Return a new graph with this graph's nodes and edges plus the given ones."""
        own_edges = ((from_node, edge) for from_node, found in self.adj.items() for edge in found)
        return RoutingGraph.from_edges([*self.adj, *nodes], [*own_edges, *edges])

    def edges_from(self, node: NodeId) -> Sequence[Edge]:
        return self.adj.get(node, ())

    def has_node(self, node: NodeId) -> bool:
        return node in self.adj

    def lookup_edges(self, from_node: NodeId, to_node: NodeId) -> Sequence[Edge]:
        return self.by_from_to.get((from_node, to_node), ())


class RoutingGraphBuilder:
    @staticmethod
    def from_requirements(
        requirements: Iterable[RoutingInfo] = entrance_to_entrance_info_collection,
        *,
        transdoors: Iterable[TransdoorConnection] | None = None,
        open_gates: bool = False,
    ) -> RoutingGraph:
        """
        Return the entrance-node graph for ``requirements``, as the module docstring describes.

        Args:
            requirements: The routing rows to turn into edges inside rooms. A row is skipped when
                either of its doors is not a node, which is what ``regions.py`` does.
            transdoors: The door crossings, which default to ``transdoor_connection_collection``.
            open_gates: Whether the two true-ending crossings get an edge with no requirement.
                By default they get no edge, so nothing past them is reachable.
        """
        crossings = tuple(transdoor_connection_collection if transdoors is None else transdoors)

        nodes: Dict[NodeId, None] = dict.fromkeys(
            door.door_identifier_unique for door in entrance_info_collection)
        for crossing in crossings:
            nodes.setdefault(crossing.from_entrance)
            nodes.setdefault(crossing.to_entrance)

        edges: List[Tuple[NodeId, Edge]] = []
        for from_node, to_node in dict.fromkeys(
                (crossing.from_entrance, crossing.to_entrance) for crossing in crossings):
            if not open_gates and (from_node, to_node) in SPECIAL_TRANSDOOR_RULES:
                continue
            edges.append((from_node, Edge(to_node=to_node, connection_number=None, req_masks=(0,))))

        for route_info in requirements:
            from_node = EntranceId.make(route_info.room_id, route_info.from_room)
            to_node = EntranceId.make(route_info.room_id, route_info.to_room)
            if from_node not in nodes or to_node not in nodes:
                continue
            masks = tuple(int(mask) for mask in route_info.get_requirement_bitmasks())
            edges.append((from_node, Edge(
                to_node=to_node,
                connection_number=route_info.connection_number,
                req_masks=masks or (0,),
                variant=route_info.variant,
            )))

        for room_id, from_neighbor, to_neighbor, ability_names in SPECIAL_ROOM_ROUTING:
            from_node = EntranceId.make(room_id, from_neighbor)
            to_node = EntranceId.make(room_id, to_neighbor)
            if from_node not in nodes or to_node not in nodes:
                continue
            mask = MaskUtils.from_flags(*(AbilityCombo[name] for name in ability_names))
            edges.append((from_node, Edge(to_node=to_node, connection_number=None,
                                          req_masks=(mask,))))

        return RoutingGraph.from_edges(nodes, edges)


# ============================================================
# Searches
# ============================================================

class RoutingQueries:
    """
    Searches over a routing graph.

    Nodes are entrance ids such as "000:003", plus the pickup and enemy nodes in the extended
    graph that ``entrances_to_items`` builds. Each edge has one or more requirement masks, and
    each mask is a set of abilities that are all needed together.
    """

    @staticmethod
    def start_entrances_for_room(graph: RoutingGraph, start_room: str) -> Tuple[NodeId, ...]:
        prefix = EntranceId.make(start_room, "")
        return tuple(sorted(node_id for node_id in graph.adj if node_id.startswith(prefix)))

    @staticmethod
    def _reachable_from(
        graph: RoutingGraph, starts: Iterable[NodeId], have_mask: ReqMask
    ) -> Set[NodeId]:
        """
        Return ``starts`` and every node reachable from them with the abilities in ``have_mask``.

        This is a breadth-first search that crosses an edge only when ``have_mask`` satisfies
        at least one of the edge's masks.
        """
        visited_nodes: Set[NodeId] = set(starts)
        frontier: deque[NodeId] = deque(visited_nodes)

        while frontier:
            current_node = frontier.popleft()
            for edge in graph.edges_from(current_node):
                if edge.to_node in visited_nodes:
                    continue
                if not MaskUtils.edge_traversable(have_mask, edge.req_masks):
                    continue
                visited_nodes.add(edge.to_node)
                frontier.append(edge.to_node)

        return visited_nodes

    @staticmethod
    def reachable_entrance_nodes(graph: RoutingGraph, start: NodeId, have_mask: ReqMask) -> Set[NodeId]:
        """
        Return ``start`` and every node reachable from it with the abilities in ``have_mask``.

        In the extended graph, the result includes the reachable pickup and enemy nodes.

        Args:
            graph: The routing graph to search.
            start: The node id to start from.
            have_mask: Bitmask of the abilities and conditions the player has.

        Returns:
            The set of reachable node ids, including ``start``.
        """
        return RoutingQueries._reachable_from(graph, (start,), have_mask)

    @staticmethod
    def reachable_entrance_nodes_from_room(
        graph: RoutingGraph,
        start_room: str,
        have_mask: ReqMask
    ) -> Set[NodeId]:
        """
        Return every node reachable from any door of ``start_room`` under ``have_mask``.

        The search starts from all of the room's entrance nodes at once. It returns an empty set
        when the graph has no entrance node in ``start_room``.
        """
        starting_entrances = RoutingQueries.start_entrances_for_room(graph, start_room)
        return RoutingQueries._reachable_from(graph, starting_entrances, have_mask)

    @staticmethod
    def reachable_with_options_bfs(
        graph: RoutingGraph,
        start: NodeId,
        goal: NodeId,
        have_mask: ReqMask,
    ) -> BFSResult:
        """
        Return a path from ``start`` to ``goal`` with the abilities in ``have_mask``.

        The search is breadth-first, so the path has the fewest possible edges.

        Args:
            graph: The routing graph to search.
            start: The node id to start from.
            goal: The node id to reach.
            have_mask: Bitmask of the abilities and conditions the player has.

        Returns:
            A BFSResult. ``reachable`` says whether ``goal`` was found, and ``path_nodes`` lists
            the nodes from ``start`` to ``goal`` with both ends included. ``steps`` describes
            each edge on the path, including which of its masks ``have_mask`` satisfies. Both
            tuples are empty when ``goal`` is unreachable.
        """
        if start == goal:
            return BFSResult(True, (start,), ())

        frontier: deque[NodeId] = deque([start])
        visited_nodes: Set[NodeId] = {start}

        # How the search reached each node: the previous node, the edge, and its usable masks
        previous_step: Dict[NodeId, Tuple[NodeId, Edge, Tuple[ReqMask, ...]]] = {}

        while frontier:
            current_node = frontier.popleft()

            for edge in graph.edges_from(current_node):
                usable_masks = MaskUtils.usable_options(have_mask, edge.req_masks)
                if not usable_masks:
                    continue

                next_node = edge.to_node
                if next_node in visited_nodes:
                    continue

                visited_nodes.add(next_node)
                previous_step[next_node] = (current_node, edge, usable_masks)

                if next_node == goal:
                    frontier.clear()
                    break

                frontier.append(next_node)

        if goal not in previous_step:
            return BFSResult(False, (), ())

        # Walk back from the goal to the start
        reconstructed_nodes_reversed: List[NodeId] = [goal]
        reconstructed_steps_reversed: List[BFSStep] = []

        current_node = goal
        while current_node != start:
            previous_node, edge_used, usable_masks_on_edge = previous_step[current_node]

            reconstructed_steps_reversed.append(
                BFSStep(
                    from_node=previous_node,
                    to_node=current_node,
                    connection_number=edge_used.connection_number,
                    variant=edge_used.variant,
                    usable_req_masks=usable_masks_on_edge,
                )
            )

            reconstructed_nodes_reversed.append(previous_node)
            current_node = previous_node

        reconstructed_nodes_reversed.reverse()
        reconstructed_steps_reversed.reverse()

        return BFSResult(
            reachable=True,
            path_nodes=tuple(reconstructed_nodes_reversed),
            steps=tuple(reconstructed_steps_reversed),
        )

    @staticmethod
    def _dominates(subset_mask: ReqMask, superset_mask: ReqMask) -> bool:
        """
        Return True when ``subset_mask`` is a subset of ``superset_mask``.
        """
        return (subset_mask | superset_mask) == superset_mask

    @staticmethod
    def _is_dominated(candidate_mask: ReqMask, frontier_masks: Set[ReqMask]) -> bool:
        """
        Return True when some mask in ``frontier_masks`` is a subset of ``candidate_mask``.
        """
        return any(
            RoutingQueries._dominates(existing_mask, candidate_mask)
            for existing_mask in frontier_masks
        )

    @staticmethod
    def _prune_and_add(candidate_mask: ReqMask, frontier_masks: Set[ReqMask]) -> None:
        """
        Add ``candidate_mask`` to ``frontier_masks``, removing every mask that it dominates.
        """
        masks_to_remove = [
            existing_mask
            for existing_mask in frontier_masks
            if RoutingQueries._dominates(candidate_mask, existing_mask)
        ]
        for removable_mask in masks_to_remove:
            frontier_masks.remove(removable_mask)

        frontier_masks.add(candidate_mask)

    @staticmethod
    def compute_min_requirements(graph: RoutingGraph, start: NodeId) -> MinReqResult:
        """
        Find the subset-minimal requirement masks for reaching each node from ``start``.

        A mask is subset-minimal when no other mask that is enough to reach the node is a subset
        of it. A node can therefore have several, such as one that needs only DJump and one that
        needs only Glide.

        Args:
            graph: The routing graph.
            start: The node id to start from.

        Returns:
            A MinReqResult. ``best`` maps each reachable node to its subset-minimal masks.
            ``parent`` maps each (node, mask) pair to the ParentInfo that
            :meth:`reconstruct_path_for_mask` follows to rebuild a path for that pair.
        """
        best_masks_by_node: Dict[NodeId, Set[ReqMask]] = {start: {0}}
        parent_by_state: Dict[Tuple[NodeId, ReqMask], ParentInfo] = {}

        # States with fewer abilities come off the heap first
        heap: List[Tuple[int, NodeId, ReqMask]] = [(0, start, 0)]

        while heap:
            _, current_node, current_requirement_mask = heapq.heappop(heap)

            node_frontier = best_masks_by_node.get(current_node)
            # Skip a stale entry, whose mask a smaller one has since replaced
            if node_frontier is None or current_requirement_mask not in node_frontier:
                continue

            for edge in graph.edges_from(current_node):
                next_node = edge.to_node
                frontier_at_next_node = best_masks_by_node.setdefault(next_node, set())

                for edge_option_requirement in edge.req_masks:
                    new_requirement_mask = current_requirement_mask | int(edge_option_requirement)

                    if RoutingQueries._is_dominated(new_requirement_mask, frontier_at_next_node):
                        continue

                    RoutingQueries._prune_and_add(new_requirement_mask, frontier_at_next_node)

                    # Record the edge that produced this (node, mask), so a path can be rebuilt
                    parent_by_state[(next_node, new_requirement_mask)] = ParentInfo(
                        prev_node=current_node,
                        prev_mask=current_requirement_mask,
                        edge_from=current_node,
                        edge_to=next_node,
                        connection_number=edge.connection_number,
                        variant=edge.variant,
                        chosen_edge_req_mask=int(edge_option_requirement),
                    )

                    heapq.heappush(
                        heap,
                        (new_requirement_mask.bit_count(), next_node, new_requirement_mask),
                    )

        return MinReqResult(best=best_masks_by_node, parent=parent_by_state)

    @staticmethod
    def masks_that_work_for_have(
        best_masks: Iterable[ReqMask],
        have_mask: ReqMask
    ) -> Tuple[ReqMask, ...]:
        """
        Return the masks in ``best_masks`` that ``have_mask`` satisfies, fewest abilities first.
        """
        viable_masks = [
            int(mask)
            for mask in best_masks
            if MaskUtils.satisfied(have_mask, int(mask))
        ]
        viable_masks.sort(key=lambda mask: (mask.bit_count(), mask))
        return tuple(viable_masks)

    @staticmethod
    def reconstruct_path_for_mask(
        parent: Mapping[Tuple[NodeId, ReqMask], ParentInfo],
        start: NodeId,
        goal: NodeId,
        req_mask: ReqMask,
    ) -> Tuple[ParentInfo, ...]:
        """
        Return one path from ``start`` to ``goal`` whose edges together require ``req_mask``.

        Each ParentInfo in the result describes one edge of the path and the mask option
        chosen on it.

        Args:
            parent: The ``parent`` mapping from :meth:`compute_min_requirements`.
            start: The node id the search started from.
            goal: The node id to reach.
            req_mask: One of the masks in ``best[goal]``.

        Returns:
            The steps in order from ``start`` to ``goal``, or an empty tuple when ``start`` is
            ``goal``.

        Raises:
            KeyError: ``parent`` has no entry for ``(goal, req_mask)``.
        """
        if start == goal:
            return ()

        reversed_steps: List[ParentInfo] = []
        current_state = (goal, int(req_mask))

        while True:
            info = parent.get(current_state)
            if info is None:
                raise KeyError(
                    f"No parent info for (node={current_state[0]!r}, mask={MaskUtils.format(current_state[1])}); "
                    f"mask may not be in best[{goal!r}]"
                )

            reversed_steps.append(info)

            # The path is complete once it leaves the start with no requirement yet
            if info.prev_node == start and info.prev_mask == 0:
                break

            current_state = (info.prev_node, info.prev_mask)

        reversed_steps.reverse()
        return tuple(reversed_steps)

    @staticmethod
    def route_options(
        graph: RoutingGraph,
        start: NodeId,
        goal: NodeId,
        *,
        have_mask: Optional[ReqMask] = None,
        limit: Optional[int] = None,
    ) -> Tuple[RouteOption, ...]:
        """
        Return the subset-minimal ways to reach ``goal``, each with one example path.

        This runs :meth:`compute_min_requirements` from ``start``, then reconstructs one path
        for each mask it found for ``goal``. Options with fewer abilities come first.

        Args:
            graph: The routing graph.
            start: The node id to start from.
            goal: The node id to reach.
            have_mask: If given, keep only the options whose ``required_mask`` it satisfies.
            limit: If given, return at most this many options, taken after sorting.

        Returns:
            A tuple of RouteOption, which is empty when ``goal`` is unreachable.
        """
        minreq_result = RoutingQueries.compute_min_requirements(graph, start)
        masks_at_goal = minreq_result.best.get(goal, set())

        if have_mask is not None:
            selected_masks = list(RoutingQueries.masks_that_work_for_have(masks_at_goal, have_mask))
        else:
            selected_masks = sorted(
                (int(mask) for mask in masks_at_goal),
                key=lambda mask: (mask.bit_count(), mask)
            )

        if limit is not None:
            selected_masks = selected_masks[: max(0, int(limit))]

        options: List[RouteOption] = []
        for required_mask in selected_masks:
            steps = RoutingQueries.reconstruct_path_for_mask(
                minreq_result.parent, start, goal, required_mask)
            options.append(RouteOption(required_mask=required_mask, steps=steps))

        return tuple(options)


# ============================================================
# Display
# ============================================================

class RoutingDisplay:
    @staticmethod
    def print_reachable_entrance_nodes(
        entrance_nodes: Iterable[NodeId],
        *,
        start_label: str,
        have_mask: ReqMask,
    ) -> None:
        nodes_sorted = sorted(set(entrance_nodes))
        print(f"Start: {start_label}")
        print(f"Have:  {MaskUtils.format(have_mask)}")
        print(f"Reachable entrance-nodes ({len(nodes_sorted)}):")
        for node_id in nodes_sorted:
            print(node_id)

    @staticmethod
    def print_bfs_result(result: BFSResult, *, have_mask: ReqMask) -> None:
        if not result.reachable:
            print(f"Unreachable with have={MaskUtils.format(have_mask)}")
            return

        print(f"Path nodes ({len(result.path_nodes)}): {' -> '.join(result.path_nodes)}")
        print("Steps:")
        for step in result.steps:
            options_text = ", ".join(MaskUtils.format(mask) for mask in step.usable_req_masks)
            print(
                f"  {step.from_node} -> {step.to_node} "
                f"(conn={step.connection_number}, variant={step.variant}) "
                f"usable: [{options_text}]"
            )
