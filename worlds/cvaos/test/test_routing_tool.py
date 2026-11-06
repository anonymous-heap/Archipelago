"""
Tests for the offline route solver in ``tools/routing``.

The solver's graph must have the same nodes and edges as the regions that generation builds, so
the last test compares what each can reach from the start.

    python -m pytest worlds/cvaos/test/test_routing_tool.py -v
"""
from __future__ import annotations

import unittest

from BaseClasses import CollectionState
from test.general import setup_multiworld

from .. import CVAOSWorld
from ..data import (
    AbilityCombo,
    enemy_meta_by_number,
    entrance_info_collection,
    entrance_to_entrance_info_collection,
    pickup_info_collection,
    transdoor_connection_collection,
)
from ..regions import _ABILITY_TO_ITEM, SPECIAL_TRANSDOOR_RULES, TFORM_ENEMY_TYPES
from ..tools.routing.entrances import (
    EntranceId,
    MaskUtils,
    RoutingGraph,
    RoutingGraphBuilder,
    RoutingQueries,
)
from ..tools.routing.entrances_to_items import (
    EnemyNodeId,
    PickupNodeId,
    default_graph,
    logic_enemy_numbers,
    pickup_route_options,
)
from ..tools.routing.spheres import (
    START_ENTRANCE,
    _SOUL_NAME_TO_ABILITY,
    ability_mask_for_item,
    build_pickup_to_ability_mask,
    build_reachability_spheres,
)

ENTRANCE_GRAPH = RoutingGraphBuilder.from_requirements()
GRAPH = default_graph()
DOORS = frozenset(door.door_identifier_unique for door in entrance_info_collection)
CROSSINGS = frozenset((crossing.from_entrance, crossing.to_entrance)
                      for crossing in transdoor_connection_collection)
PORTALS = frozenset(node for crossing in CROSSINGS for node in crossing) - DOORS


def _room(node: str) -> str:
    return node.split(":", 1)[0]


def _edges(graph: RoutingGraph):
    return [(from_node, edge) for from_node, edges in graph.adj.items() for edge in edges]


class EntranceGraphTest(unittest.TestCase):
    def test_every_node_is_a_door_portal_pickup_or_enemy(self) -> None:
        pickups = {PickupNodeId.make(pickup.pickup_number) for pickup in pickup_info_collection}
        enemies = {EnemyNodeId.make(number) for number in logic_enemy_numbers()}
        self.assertEqual(set(GRAPH.adj), DOORS | PORTALS | pickups | enemies)

    def test_routing_rows_run_between_doors_of_their_room(self) -> None:
        # Each row starts at the door Soma enters RoomID by, which is "RoomID:From".
        for row in entrance_to_entrance_info_collection:
            from_node = EntranceId.make(row.room_id, row.from_room)
            to_node = EntranceId.make(row.room_id, row.to_room)
            if from_node not in ENTRANCE_GRAPH.adj or to_node not in ENTRANCE_GRAPH.adj:
                continue
            numbers = [edge.connection_number
                       for edge in ENTRANCE_GRAPH.lookup_edges(from_node, to_node)]
            self.assertIn(row.connection_number, numbers, f"row {row.connection_number}")

    def test_a_row_naming_a_tagged_door_has_an_edge(self) -> None:
        edges = ENTRANCE_GRAPH.lookup_edges("103:104 (left)", "103:104 (right)")
        self.assertEqual([edge.connection_number for edge in edges], [155])

    def test_a_row_without_masks_is_open(self) -> None:
        # Row 565 names only the true-ending condition, which is a combo with no flag, so it has
        # no mask. Generation gates the 507 -> 506 crossing in code instead.
        row = next(row for row in entrance_to_entrance_info_collection
                   if row.connection_number == 565)
        self.assertEqual(row.get_requirement_bitmasks(), ())
        edges = ENTRANCE_GRAPH.lookup_edges("507:505", "507:506")
        self.assertEqual([edge.req_masks for edge in edges], [(0,)])

    def test_every_edge_between_rooms_is_a_door_crossing(self) -> None:
        # Only the transdoor table moves a search into another room, so a crossing that the
        # override file removes stays closed.
        for from_node, edge in _edges(ENTRANCE_GRAPH):
            if _room(from_node) != _room(edge.to_node):
                self.assertIn((from_node, edge.to_node), CROSSINGS)

    def test_door_crossings_have_no_requirement(self) -> None:
        for crossing in CROSSINGS - set(SPECIAL_TRANSDOOR_RULES):
            edges = ENTRANCE_GRAPH.lookup_edges(*crossing)
            self.assertIn((0,), [edge.req_masks for edge in edges], crossing)

    def test_the_true_ending_gates_are_closed_unless_opened(self) -> None:
        opened = RoutingGraphBuilder.from_requirements(open_gates=True)
        for crossing in SPECIAL_TRANSDOOR_RULES:
            self.assertFalse(ENTRANCE_GRAPH.lookup_edges(*crossing))
            self.assertEqual([edge.req_masks for edge in opened.lookup_edges(*crossing)], [(0,)])

    def test_pickup_and_enemy_nodes_have_no_way_out(self) -> None:
        for node, edges in GRAPH.adj.items():
            if PickupNodeId.is_pickup(node) or EnemyNodeId.is_enemy(node):
                self.assertEqual(list(edges), [], node)


class QueryTest(unittest.TestCase):
    def test_double_jump_reaches_more_than_nothing(self) -> None:
        with_nothing = RoutingQueries.reachable_entrance_nodes(GRAPH, START_ENTRANCE, 0)
        with_djump = RoutingQueries.reachable_entrance_nodes(
            GRAPH, START_ENTRANCE, int(AbilityCombo.DJump))
        self.assertIn(START_ENTRANCE, with_nothing)
        self.assertLess(with_nothing, with_djump)

    def test_route_options_are_real_paths(self) -> None:
        goal = PickupNodeId.make(42)
        options = pickup_route_options(GRAPH, START_ENTRANCE, 42)
        self.assertTrue(options)
        for option in options:
            self.assertEqual(option.steps[0].edge_from, START_ENTRANCE)
            self.assertEqual(option.steps[-1].edge_to, goal)
            combined = 0
            for step, following in zip(option.steps, option.steps[1:]):
                self.assertEqual(step.edge_to, following.edge_from)
            for step in option.steps:
                masks = [mask for edge in GRAPH.lookup_edges(step.edge_from, step.edge_to)
                         for mask in edge.req_masks]
                self.assertIn(step.chosen_edge_req_mask, masks)
                combined |= step.chosen_edge_req_mask
            self.assertEqual(combined, option.required_mask, MaskUtils.format(combined))

            path = RoutingQueries.reachable_with_options_bfs(
                GRAPH, START_ENTRANCE, goal, option.required_mask)
            self.assertTrue(path.reachable)


class SphereTest(unittest.TestCase):
    def test_every_soul_in_the_map_is_a_pickup_and_matches_generation(self) -> None:
        pickup_names = {pickup.simple_name for pickup in pickup_info_collection}
        for name, ability in _SOUL_NAME_TO_ABILITY.items():
            self.assertIn(name, pickup_names)
            self.assertEqual(_ABILITY_TO_ITEM[ability], name)
        for ability, name in _ABILITY_TO_ITEM.items():
            if name in pickup_names:
                self.assertEqual(_SOUL_NAME_TO_ABILITY.get(name), ability, name)

    def test_the_galamoth_pickup_grants_galamoth(self) -> None:
        galamoth = next(pickup.pickup_number for pickup in pickup_info_collection
                        if pickup.simple_name == "Galamoth")
        self.assertEqual(build_pickup_to_ability_mask()[galamoth], int(AbilityCombo.Galamoth))

    def test_spheres_from_the_start(self) -> None:
        pickup_to_abilities = build_pickup_to_ability_mask()
        result = build_reachability_spheres(GRAPH, START_ENTRANCE, pickup_to_abilities)
        self.assertTrue(result.spheres)
        reached = [pickup for sphere in result.spheres for pickup in sphere.newly_reachable_pickups]
        self.assertEqual(len(reached), len(set(reached)), "a pickup appears in two spheres")
        self.assertTrue(result.final_have_mask & int(AbilityCombo.Galamoth))
        # No pickup grants an enemy-only soul, so neither bit can appear.
        self.assertFalse(result.final_have_mask & int(AbilityCombo.Kick | AbilityCombo.Tform))


SOULS = ("Flying Armor", "Skeleton Blaze", "Malphas", "Hippogryph", "Undine", "Skula",
         "Black Panther", "Giant Bat", "Grave Keeper", "Galamoth")
ANCIENT_BOOKS = ("Ancient Book 1", "Ancient Book 2", "Ancient Book 3")


class GenerationAgreementTest(unittest.TestCase):
    """
    The solver reaches the same pickups from the start as generation does, for several item sets.

    Generation runs with the Forbidden Area button left in place, so the A01 barrier follows the
    CSV rule as the solver does. It also includes the Hard Mode pickups, so that every pickup is a
    location.

    Each item set becomes the solver's ``have_mask`` through ``ability_mask_for_item``. Kick and
    Chronomage have no item, so generation never meets them, and the mask leaves them out.
    Generation meets Tform by reaching a Devil or Manticore, so the solver adds Tform once its
    search reaches one of their enemy nodes, and then searches again.

    The true-ending gate stays closed in generation without the Ancient Books, and the solver's
    default graph leaves the gated crossings out. With every soul and the books, generation opens
    the gate, which the solver matches with ``open_gates``.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.multiworld = setup_multiworld(
            CVAOSWorld, ("generate_early", "create_regions"), seed=1,
            options={"forbidden_area_button": "off", "hard_mode_pickups": "always"})
        cls.world = cls.multiworld.worlds[1]
        cls.pickup_by_location = {pickup.location_name: pickup.pickup_number
                                  for pickup in pickup_info_collection}

    def generation_reaches(self, items: tuple[str, ...]) -> set[int]:
        state = CollectionState(self.multiworld)
        for name in items:
            state.collect(self.world.create_item(name), prevent_sweep=True)
        return {self.pickup_by_location[location.name]
                for location in self.multiworld.get_locations(self.world.player)
                if location.name in self.pickup_by_location and location.can_reach(state)}

    @staticmethod
    def solver_reaches(graph: RoutingGraph, items: tuple[str, ...]) -> tuple[set[int], int]:
        """Return the pickups the solver reaches with ``items``, and the final ``have_mask``."""
        have_mask = 0
        for name in items:
            have_mask |= ability_mask_for_item(name)
        while True:
            nodes = RoutingQueries.reachable_entrance_nodes(graph, START_ENTRANCE, have_mask)
            tform_source = any(
                EnemyNodeId.is_enemy(node)
                and enemy_meta_by_number[EnemyNodeId.number(node)][0] in TFORM_ENEMY_TYPES
                for node in nodes)
            if not tform_source or have_mask & int(AbilityCombo.Tform):
                pickups = {PickupNodeId.number(node) for node in nodes
                           if PickupNodeId.is_pickup(node)}
                return pickups, have_mask
            have_mask |= int(AbilityCombo.Tform)

    def assert_agree(self, items: tuple[str, ...], graph: RoutingGraph = GRAPH) -> int:
        """Assert that both reach the same pickups, and return the solver's final ``have_mask``."""
        solver, have_mask = self.solver_reaches(graph, items)
        generation = self.generation_reaches(items)
        self.assertTrue(generation, "generation reaches nothing")
        self.assertEqual(sorted(solver - generation), [], "only the solver reaches these")
        self.assertEqual(sorted(generation - solver), [], "only generation reaches these")
        return have_mask

    def test_no_items(self) -> None:
        self.assertFalse(self.assert_agree(()))

    def test_double_jump(self) -> None:
        self.assert_agree(("Malphas",))

    def test_glide_dive_and_water_walk(self) -> None:
        self.assert_agree(("Flying Armor", "Skula", "Undine"))

    def test_double_jump_water_walk_and_dive_reach_a_transformation_soul(self) -> None:
        # These three reach a Devil or Manticore, and four pickups need Tform on top of them.
        have_mask = self.assert_agree(("Malphas", "Undine", "Skula"))
        self.assertTrue(have_mask & int(AbilityCombo.Tform))

    def test_every_soul_with_a_pickup(self) -> None:
        have_mask = self.assert_agree(SOULS)
        self.assertTrue(have_mask & int(AbilityCombo.Tform))

    def test_every_soul_and_the_ancient_books(self) -> None:
        self.assert_agree(SOULS + ANCIENT_BOOKS, default_graph(open_gates=True))


if __name__ == "__main__":
    unittest.main()
