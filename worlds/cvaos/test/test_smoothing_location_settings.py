"""
Item smoothing must respect the player's location settings and plando placements.

The pre-fill bias and the post-fill refine passes move progression items on their own, outside the
main fill, so they have to apply the rules the main fill applies: no progression in an excluded
location, a priority location keeps its progression, a plando'd item stays put, and an item is
never moved somewhere that needs that same item to reach.

    python -m pytest worlds/cvaos/test/test_smoothing_location_settings.py -v
"""
from __future__ import annotations

import random
import unittest
from unittest import mock

from BaseClasses import CollectionState, LocationProgressType, MultiWorld
from Fill import (distribute_items_restrictive, distribute_planned_blocks, parse_planned_blocks,
                  resolve_early_locations_for_planned)
from test.general import setup_multiworld
from worlds.AutoWorld import call_all
from worlds.generic.Rules import exclusion_rules

from .. import CVAOSWorld
from ..item_smoothing import (ANCIENT_BOOK_NAMES, BIASED_SOULS, BLACK_PANTHER, MOVEMENT_SOULS,
                              _region_depth, _soul_tiers, _state_without,
                              smoothable_locations_in_order)
from ..items import logic_only_progression_names
from ..locations import location_name_to_id

BEFORE_FILL = ("generate_early", "create_regions", "create_items", "set_rules",
               "connect_entrances", "generate_basic")
LOCATION_NAMES = sorted(location_name_to_id)
SMOOTHED = (*BIASED_SOULS, BLACK_PANTHER, *ANCIENT_BOOK_NAMES)


def generate(seed: int, options: dict) -> MultiWorld:
    """Generate one world the way Main does, including its location settings and item plando."""
    multiworld = setup_multiworld(CVAOSWorld, BEFORE_FILL[:4], seed=seed,
                                  options={"item_smoothing": "strict", **options})
    world = multiworld.worlds[1]
    excluded = world.options.exclude_locations.value
    exclusion_rules(multiworld, 1, excluded)
    existing = {location.name for location in multiworld.get_locations(1)}
    for name in (world.options.priority_locations.value - excluded) & existing:
        multiworld.get_location(name, 1).progress_type = LocationProgressType.PRIORITY
    multiworld.plando_item_blocks = parse_planned_blocks(multiworld)
    for step in BEFORE_FILL[4:]:
        call_all(multiworld, step)
    resolve_early_locations_for_planned(multiworld)
    distribute_planned_blocks(multiworld, multiworld.plando_item_blocks[1])
    call_all(multiworld, "pre_fill")
    distribute_items_restrictive(multiworld)
    call_all(multiworld, "post_fill")
    return multiworld


class LocationSettingsTest(unittest.TestCase):
    def test_no_progression_in_an_excluded_location(self):
        for seed in range(3):
            excluded = set(random.Random(seed).sample(LOCATION_NAMES, 30))
            multiworld = generate(seed, {"exclude_locations": excluded})
            with self.subTest(seed=seed):
                wrong = [loc.name for loc in multiworld.get_locations(1)
                         if loc.progress_type == LocationProgressType.EXCLUDED and loc.item.advancement]
                self.assertEqual(wrong, [])

    def test_priority_locations_keep_progression(self):
        for seed in range(3):
            priority = set(random.Random(100 + seed).sample(LOCATION_NAMES, 20))
            multiworld = generate(seed, {"priority_locations": priority})
            with self.subTest(seed=seed):
                names = {loc.name for loc in multiworld.get_locations(1)}
                marked = {loc.name for loc in multiworld.get_locations(1)
                          if loc.progress_type == LocationProgressType.PRIORITY}
                self.assertEqual(marked, priority & names)
                wrong = [loc.name for loc in multiworld.get_locations(1)
                         if loc.progress_type == LocationProgressType.PRIORITY
                         and not loc.item.advancement]
                self.assertEqual(wrong, [])

    def test_many_priority_locations_leave_the_smoothing_items_to_the_main_fill(self):
        # With nearly every progression item needed for a priority location, smoothing holding some
        # back made the main fill run out. Seeds 4, 5 and 9 failed to generate that way.
        for seed in (4, 5, 9):
            priority = set(random.Random(1000 + seed).sample(LOCATION_NAMES, 30))
            multiworld = generate(seed, {"priority_locations": priority})
            with self.subTest(seed=seed):
                self.assertFalse(multiworld.worlds[1]._smoothing_items)
                unmet = [loc.name for loc in multiworld.get_locations(1)
                         if loc.progress_type == LocationProgressType.PRIORITY
                         and not loc.item.advancement]
                self.assertEqual(unmet, [])

    def test_post_fill_smoothing_leaves_a_priority_location_alone(self):
        # A ceiling or floor breaker is progression that the post-fill pass may reorder, so a
        # priority location holding one must not be among the locations it reorders.
        multiworld = generate(3, {})
        breaker = next(loc for loc in multiworld.get_filled_locations(1)
                       if loc.item.player == 1 and not loc.locked
                       and loc.item.name in logic_only_progression_names)
        breaker.progress_type = LocationProgressType.PRIORITY
        ordered = smoothable_locations_in_order(multiworld, 1, list(multiworld.get_spheres()),
                                                _region_depth(multiworld, 1))
        self.assertTrue(ordered)
        self.assertNotIn(breaker, ordered)

    def test_the_lateness_tiers_count_each_soul_as_it_is_collected(self):
        # The pre-fill passes rank locations by the step at which they become reachable as the
        # movement souls are added. Without a fresh reachability check after each soul, every
        # location after the first step looked unreachable.
        multiworld = setup_multiworld(CVAOSWorld, BEFORE_FILL, seed=2)
        world = multiworld.worlds[1]
        pool = [*multiworld.itempool, *world.get_pre_fill_items()]
        souls = [item for item in pool if item.player == 1 and item.name in MOVEMENT_SOULS]
        depth = _region_depth(multiworld, 1)
        locations = [loc for loc in multiworld.get_locations(1)
                     if loc.address is not None and loc.parent_region.name in depth]
        tiers = _soul_tiers(multiworld, 1, pool, souls, locations)
        self.assertGreater(len(set(tiers.values())), 2, tiers)

    def test_a_plandoed_item_stays_where_it_was_put_and_is_not_placed_twice(self):
        for name, target in (("Ancient Book 3", "Entrance Parapet [000 Lucky Charm]"),
                             ("Giant Bat", "Entrance Parapet [000 Lucky Charm]")):
            for seed in range(3):
                multiworld = generate(seed, {"plando_items": [{"item": name, "location": target}]})
                with self.subTest(item=name, seed=seed):
                    self.assertEqual(multiworld.get_location(target, 1).item.name, name)
                    copies = [loc for loc in multiworld.get_filled_locations(1)
                              if loc.item.name == name]
                    self.assertEqual(len(copies), 1)

    def test_a_moved_item_is_never_behind_itself_even_with_minimal_accessibility(self):
        for seed in (503, 504, 505):
            multiworld = generate(seed, {"accessibility": "minimal", "goal": "graham",
                                         "early_mobility_souls": "guarantee_early",
                                         "item_smoothing_order": "spheres"})
            everything = CollectionState(multiworld)
            everything.sweep_for_advancements()
            with self.subTest(seed=seed):
                stranded = [loc.item.name for loc in multiworld.get_locations(1)
                            if loc.item.name in SMOOTHED and not loc.can_reach(everything)]
                self.assertEqual(stranded, [])

    def test_the_state_a_moved_item_is_checked_against_never_holds_that_item(self):
        # The refine passes only move an item to a location reachable in this state, which is what
        # keeps the item from landing behind itself.
        multiworld = generate(5, {})
        for name in ("Malphas", "Hippogryph", "Giant Bat", "Ancient Book 1"):
            location = next(loc for loc in multiworld.get_filled_locations(1)
                            if loc.item.name == name)
            with self.subTest(item=name):
                state = _state_without(multiworld, location)
                self.assertFalse(state.has(name, 1))
                self.assertTrue(state.prog_items[1], "the state should still hold other items")


    def test_the_refine_passes_try_only_locations_reachable_without_the_item(self):
        # With a target at the very end of the run, the nearest candidates are late locations that
        # need the moved item, so a pass checking against a state that still holds it tries them.
        from .. import item_smoothing
        multiworld = generate(5, {})
        world = multiworld.worlds[1]
        tried = []

        def record(_multiworld, own_loc, dest):
            tried.append((own_loc, dest))
            return False

        with mock.patch.object(item_smoothing, "_swap_if_safe", record):
            world._soul_targets = {"Malphas": 1.0}
            item_smoothing.refine_soul_positions(world)
            with mock.patch.object(world.random, "uniform", lambda low, high: 1.0):
                item_smoothing.refine_ancient_books(world)
        self.assertEqual({own.item.name for own, _dest in tried} - {"Malphas"},
                         set(ANCIENT_BOOK_NAMES), "both passes must have tried moves")
        for own_loc, dest in tried:
            without = CollectionState(multiworld)
            without.sweep_for_advancements(
                locations=[loc for loc in multiworld.get_filled_locations() if loc is not own_loc])
            with self.subTest(item=own_loc.item.name, dest=dest.name):
                self.assertTrue(dest.can_reach(without))


class PoolTest(unittest.TestCase):
    """Item smoothing keeps the items it places out of the multiworld pool.

    A world must not change the pool after create_items, so the items that smoothing places in
    pre_fill never enter it. Items that the core needs in the pool, for an item link, a plando or
    non_local_items, stay there. An item that a pre-fill pass cannot place goes into the pool once,
    for the main fill.
    """

    def test_the_pool_does_not_change_after_create_items(self):
        options = {"item_smoothing": "strict", "early_mobility_souls": "guarantee_early",
                   "black_panther_bias": True}
        for seed in range(3):
            multiworld = setup_multiworld(CVAOSWorld, BEFORE_FILL[:3], seed=seed, options=options)
            created = list(multiworld.itempool)
            with self.subTest(seed=seed):
                for step in (*BEFORE_FILL[3:], "pre_fill"):
                    call_all(multiworld, step)
                    self.assertEqual(multiworld.itempool, created,
                                     f"the pool changed during {step}")
                held = {item.name for item in multiworld.worlds[1]._smoothing_items}
                self.assertEqual(held, set(SMOOTHED))
                self.assertEqual(multiworld.worlds[1].get_pre_fill_items(), [],
                                 "pre_fill must place every held-back item")

    def test_no_pre_placed_item_needs_itself_to_be_reached(self):
        # Right after pre_fill, before the post-fill refine moves anything: collect the pool and
        # sweep, which picks up each pre-placed item once its location is reachable.
        options = {"item_smoothing": "strict", "early_mobility_souls": "guarantee_early",
                   "black_panther_bias": True}
        for seed in range(5):
            multiworld = setup_multiworld(CVAOSWorld, (*BEFORE_FILL, "pre_fill"), seed=seed,
                                          options=options)
            state = CollectionState(multiworld)
            for item in multiworld.itempool:
                multiworld.worlds[1].collect(state, item)
            state.sweep_for_advancements()
            with self.subTest(seed=seed):
                stranded = [loc.item.name for loc in multiworld.get_filled_locations(1)
                            if loc.item.name in SMOOTHED and not loc.can_reach(state)]
                self.assertEqual(stranded, [])

    def two_players(self, first: dict) -> MultiWorld:
        multiworld = setup_multiworld([CVAOSWorld, CVAOSWorld], (), seed=7,
                                      options=[{"item_smoothing": "strict", **first},
                                               {"item_smoothing": "strict"}])
        multiworld.set_item_links()
        for step in BEFORE_FILL:
            call_all(multiworld, step)
        return multiworld

    def test_a_non_local_soul_stays_in_the_pool(self):
        multiworld = self.two_players({"non_local_items": ["Giant Bat"]})
        self.assertIn("Giant Bat", [item.name for item in multiworld.itempool if item.player == 1])
        self.assertNotIn("Giant Bat", [item.name for item in multiworld.worlds[1]._smoothing_items])

    def test_a_linked_soul_stays_in_the_pool(self):
        link = {"item_links": [{"name": "Bats", "item_pool": ["Giant Bat"],
                                "replacement_item": None, "link_replacement": False}]}
        multiworld = setup_multiworld([CVAOSWorld, CVAOSWorld], (), seed=7,
                                      options=[{"item_smoothing": "strict", **link}] * 2)
        multiworld.set_item_links()
        for step in BEFORE_FILL:
            call_all(multiworld, step)
        for player in (1, 2):
            with self.subTest(player=player):
                self.assertIn("Giant Bat",
                              [item.name for item in multiworld.itempool if item.player == player])
                self.assertNotIn("Giant Bat", [item.name for item
                                               in multiworld.worlds[player]._smoothing_items])

    def test_a_world_with_every_location_excluded_still_generates(self):
        # Nothing can be pre-placed in the first world, so all of its held-back items go to the
        # main fill, which places their progression in the second world.
        multiworld = setup_multiworld([CVAOSWorld, CVAOSWorld], (), seed=1,
                                      options=[{"item_smoothing": "strict",
                                                "exclude_locations": LOCATION_NAMES},
                                               {"item_smoothing": "strict"}])
        for step in BEFORE_FILL:
            call_all(multiworld, step)
        exclusion_rules(multiworld, 1, set(LOCATION_NAMES))
        call_all(multiworld, "pre_fill")
        self.assertEqual(multiworld.worlds[1].get_pre_fill_items(), [])
        distribute_items_restrictive(multiworld)
        call_all(multiworld, "post_fill")
        self.assertEqual(list(multiworld.get_unfilled_locations()), [])
        for name in SMOOTHED:
            with self.subTest(item=name):
                copies = [loc for loc in multiworld.get_filled_locations()
                          if loc.item.player == 1 and loc.item.name == name]
                self.assertEqual(len(copies), 1)
        self.assertTrue(multiworld.can_beat_game(CollectionState(multiworld)))

    def test_an_item_that_cannot_be_pre_placed_goes_to_the_main_fill_once(self):
        multiworld = setup_multiworld(CVAOSWorld, BEFORE_FILL, seed=11,
                                      options={"item_smoothing": "strict",
                                               "black_panther_bias": True})
        with mock.patch("Fill.fill_restrictive"), \
                mock.patch("worlds.cvaos.item_smoothing._place_in_band", return_value=False):
            call_all(multiworld, "pre_fill")
        self.assertEqual(multiworld.worlds[1].get_pre_fill_items(), [])
        distribute_items_restrictive(multiworld)
        call_all(multiworld, "post_fill")
        for name in SMOOTHED:
            with self.subTest(item=name):
                copies = [loc for loc in multiworld.get_filled_locations(1)
                          if loc.item.name == name]
                self.assertEqual(len(copies), 1)
        self.assertTrue(multiworld.can_beat_game(CollectionState(multiworld)))


if __name__ == "__main__":
    unittest.main()
