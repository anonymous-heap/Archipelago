"""
Tests for item smoothing (see data/item_balancing/README.md).

With item_smoothing on, the Ancient Books are spread over the run, the mobility souls and Black
Panther are aimed at early or late targets, and more desirable non-progression items are moved
later in the run. With item_smoothing off, none of this happens.

    python -m pytest worlds/cvaos/test/test_item_balancing.py -v
"""
from __future__ import annotations

import random
import statistics
import unittest
from collections import Counter

from BaseClasses import CollectionState, MultiWorld
from Fill import distribute_items_restrictive
from test.general import gen_steps, setup_multiworld
from worlds.AutoWorld import call_all

from .. import CVAOSWorld
from ..data import item_balancing as desirability_data
from ..item_smoothing import (ANCIENT_BOOK_NAMES, BIASED_SOULS, EARLY_BAND, EARLY_GUARANTEE,
                              UNREACHED_DEPTH, SmoothingParams, _assign_targets,
                              _designate_early_soul, _run_position, region_depth,
                              smoothable_locations_in_order)
from ..items import CVAOSItem, item_table
from ..regions import can_reach_room
from .test_smoothing_location_settings import BEFORE_FILL, generate

# The post-fill pass aims one book into each of [0, 1/3], [1/3, 2/3] and [1/2, 3/4] of the run, so
# the books in run order belong in these bands. A swap can stop short of its target, so each band
# here is widened by 0.05 to 0.1.
BOOK_BANDS = ((0.0, 0.4), (0.25, 0.75), (0.45, 0.85))


def prepared(seed: int, options: dict) -> CVAOSWorld:
    """Generate one world through pre_fill, where the smoothing passes set their targets."""
    return setup_multiworld(CVAOSWorld, gen_steps, seed=seed, options=options).worlds[1]


def book_positions(multiworld: MultiWorld) -> list[float]:
    """Return the run positions of the three Ancient Books, earliest first."""
    position = _run_position(multiworld, 1)
    return sorted(position[loc] for loc in position if loc.item.name in ANCIENT_BOOK_NAMES)


def positions_by_desirability(multiworld: MultiWorld, by_map_distance: bool) -> list[float]:
    """Return the positions of the rated items that the post-fill pass reorders, least desirable
    first.

    A position is the item's rank in that pass's order, scaled to [0, 1]. Gold is left out, because
    the pass places gold by amount rather than by desirability.
    """
    ordered = smoothable_locations_in_order(multiworld, 1, list(multiworld.get_spheres()),
                                            region_depth(multiworld, 1),
                                            by_map_distance=by_map_distance)
    last = max(1, len(ordered) - 1)
    rated = []
    for rank, loc in enumerate(ordered):
        info = desirability_data.by_name.get(loc.item.name)
        if info is not None and info.category != "money":
            rated.append((info.desirability, rank / last))
    rated.sort(key=lambda pair: pair[0])
    return [position for _desirability, position in rated]


class DefaultOptionsTest(unittest.TestCase):
    """Seeds generated and filled with the default options.

    The default goal is chaos, and the gate on the way to Chaos needs all three Ancient Books.
    Smoothing is strict, and early_mobility_souls is bias_early, which never picks Giant Bat as
    the early soul.
    """

    SEEDS = range(9, 14)

    @classmethod
    def setUpClass(cls) -> None:
        cls.multiworlds = {seed: generate(seed, {}) for seed in cls.SEEDS}

    def test_every_seed_is_beatable_with_the_books_required(self):
        for seed, multiworld in self.multiworlds.items():
            with self.subTest(seed=seed):
                self.assertTrue(multiworld.fulfills_accessibility())

    def test_the_books_are_placed_and_locked_before_the_main_fill(self):
        # A book that pre_fill could not place would go into the pool, unlocked, for the main fill.
        for seed, multiworld in self.multiworlds.items():
            books = [loc for loc in multiworld.get_filled_locations(1)
                     if loc.item.name in ANCIENT_BOOK_NAMES]
            with self.subTest(seed=seed):
                self.assertEqual(sorted(loc.item.name for loc in books), list(ANCIENT_BOOK_NAMES))
                self.assertTrue(all(loc.locked for loc in books))
                self.assertEqual([item for item in multiworld.itempool
                                  if item.name in ANCIENT_BOOK_NAMES], [])

    def test_the_books_are_spread_over_the_run(self):
        for seed, multiworld in self.multiworlds.items():
            positions = book_positions(multiworld)
            with self.subTest(seed=seed):
                self.assertEqual(len(positions), 3)
                for position, (low, high) in zip(positions, BOOK_BANDS):
                    self.assertTrue(low <= position <= high, positions)

    def test_giant_bat_is_aimed_late_and_usually_lands_late(self):
        positions = []
        for seed, multiworld in self.multiworlds.items():
            targets = multiworld.worlds[1]._soul_targets
            self.assertGreater(targets["Giant Bat"], EARLY_BAND, f"seed {seed}")
            run_position = _run_position(multiworld, 1)
            positions.append(next(position for loc, position in run_position.items()
                                  if loc.item.name == "Giant Bat"))
        self.assertGreater(statistics.median(positions), 0.5, positions)


class SmoothingOrderTest(unittest.TestCase):
    """The post-fill pass along each item_smoothing_order axis, on the same seed."""

    ORDERS = ("spheres", "map_distance")

    @classmethod
    def setUpClass(cls) -> None:
        cls.multiworlds = {order: generate(4, {"item_smoothing_order": order})
                           for order in cls.ORDERS}

    def test_more_desirable_items_land_later(self):
        for order, multiworld in self.multiworlds.items():
            positions = positions_by_desirability(multiworld,
                                                  by_map_distance=order == "map_distance")
            with self.subTest(order=order):
                self.assertGreater(len(positions), 10, "too few rated items to compare")
                half = len(positions) // 2
                gap = statistics.mean(positions[half:]) - statistics.mean(positions[:half])
                self.assertGreater(gap, 0.2)

    def test_the_two_orders_give_different_arrangements(self):
        # Pre_fill and the main fill do not read the option, so only the post-fill pass differs.
        arrangements = [{loc.name: loc.item.name for loc in multiworld.get_filled_locations(1)}
                        for multiworld in self.multiworlds.values()]
        self.assertNotEqual(*arrangements)

    def test_map_distance_order_follows_map_depth(self):
        multiworld = self.multiworlds["map_distance"]
        depth = region_depth(multiworld, 1)
        ordered = smoothable_locations_in_order(multiworld, 1, list(multiworld.get_spheres()),
                                                depth, by_map_distance=True)
        self.assertGreater(len(ordered), 10)
        depths = [depth.get(loc.parent_region.name, UNREACHED_DEPTH) for loc in ordered]
        self.assertEqual(depths, sorted(depths))


class GoldTargetTest(unittest.TestCase):
    """Where the post-fill pass aims gold.

    The jitter is turned off, so each target is the position in the run that the item is aimed at.
    """

    NO_JITTER = SmoothingParams(nu=4.0, sigma_base=0.0, k=0.0, eps=0.0)
    RATED = [row.name for row in desirability_data.rows if row.category != "money"]

    def targets(self, names: list[str]) -> list[tuple[str, float]]:
        items = [CVAOSItem(name, item_table[name].classification, item_table[name].code, 1)
                 for name in names]
        return list(zip(names, _assign_targets(items, self.NO_JITTER, random.Random(1))))

    def test_gold_is_spread_over_the_run_and_one_larger_copy_is_aimed_last(self):
        for amount in ("500 Gold", "1000 Gold", "2000 Gold"):
            targets = self.targets([*self.RATED, *["100 Gold"] * 8, *[amount] * 3])
            rated = [target for name, target in targets if name in self.RATED]
            small = [target for name, target in targets if name == "100 Gold"]
            large = sorted(target for name, target in targets if name == amount)
            with self.subTest(amount=amount):
                # One copy of the larger amount is aimed after every rated item.
                self.assertGreater(large[-1], max(rated))
                # Every other copy, including every 100 Gold, is stratified over the whole run, so
                # each of that many equal slices of the run holds exactly one.
                spread = [*small, *large[:-1]]
                slices = sorted(int(target * len(spread)) for target in spread)
                self.assertEqual(slices, list(range(len(spread))))


class EarlySoulChoiceTest(unittest.TestCase):
    """The choice of the run's early mobility soul, drawn many times from fixed seeds."""

    def _picks(self, guarantee: float, names=BIASED_SOULS) -> Counter:
        return Counter(_designate_early_soul(random.Random(seed), list(names), guarantee)
                       for seed in range(2000))

    def test_the_early_soul_is_usually_malphas_and_otherwise_hippogryph(self):
        picks = self._picks(1.0)
        self.assertEqual(set(picks), {"Malphas", "Hippogryph"})
        self.assertGreater(picks["Malphas"], 3 * picks["Hippogryph"])

    def test_bias_early_leaves_some_runs_without_an_early_soul(self):
        fraction = self._picks(EARLY_GUARANTEE)[None] / 2000
        self.assertTrue(0.04 < fraction < 0.10, fraction)

    def test_giant_bat_alone_gives_no_early_soul(self):
        # Plando or an item link can take Malphas and Hippogryph out of the pool.
        self.assertEqual(self._picks(1.0, names=["Giant Bat"]), Counter({None: 2000}))


class SoulTargetTest(unittest.TestCase):
    """The lateness targets that pre_fill gives the mobility souls and Black Panther."""

    def test_early_mobility_souls_off_leaves_the_souls_in_the_pool(self):
        world = prepared(555, {"early_mobility_souls": "off", "black_panther_bias": False})
        self.assertIsNone(getattr(world, "_soul_targets", None))
        pool_souls = {item.name for item in world.multiworld.itempool if item.name in BIASED_SOULS}
        self.assertEqual(pool_souls, set(BIASED_SOULS))

    def test_guarantee_early_aims_malphas_or_hippogryph_early_and_giant_bat_late(self):
        for seed in (900, 901):
            world = prepared(seed, {"early_mobility_souls": "guarantee_early",
                                    "black_panther_bias": False})
            targets = world._soul_targets
            early = min(targets, key=targets.get)
            with self.subTest(seed=seed):
                self.assertIn(early, ("Malphas", "Hippogryph"))
                self.assertLess(targets[early], EARLY_BAND)
                self.assertGreater(targets["Giant Bat"], EARLY_BAND)

    def test_black_panther_bias_gives_black_panther_a_target_in_every_seed(self):
        # BLACK_PANTHER_EARLY_CHANCE is 1.0, so the bias applies in every seed.
        for seed in (700, 701):
            with self.subTest(seed=seed):
                self.assertIn("Black Panther", prepared(seed, {})._soul_targets)

    def test_black_panther_bias_off_gives_black_panther_no_target(self):
        world = prepared(606, {"black_panther_bias": False})
        self.assertNotIn("Black Panther", getattr(world, "_soul_targets", None) or {})


class SmoothingOffTest(unittest.TestCase):
    def test_off_leaves_the_books_in_the_pool(self):
        world = prepared(42, {"item_smoothing": "off"})
        books = [item for item in world.multiworld.itempool if item.name in ANCIENT_BOOK_NAMES]
        self.assertEqual(len(books), 3)


class FillTest(unittest.TestCase):
    """One seed of each non-default smoothing setting fills completely and stays accessible.

    DefaultOptionsTest covers the default options.
    """

    SETTINGS = (
        {"item_smoothing_order": "spheres"},
        {"early_mobility_souls": "off", "black_panther_bias": False},
        {"early_mobility_souls": "guarantee_early", "black_panther_bias": False},
        {"black_panther_bias": False},
        {"item_smoothing": "off"},
    )

    def test_each_setting_fills_an_accessible_seed(self):
        for options in self.SETTINGS:
            with self.subTest(**options):
                multiworld = generate(1, options)
                self.assertEqual(list(multiworld.get_unfilled_locations(1)), [])
                self.assertTrue(multiworld.fulfills_accessibility())


class MultiworldDeterminismTest(unittest.TestCase):
    """Two cvaos slots with smoothing on get the same placement from the same seed.

    Each slot's smoothing orders only the locations in its own world, so the result does not depend
    on the iteration order of a set that mixes both worlds' locations.
    """

    def test_two_slots_get_the_same_placement_from_the_same_seed(self):
        def placement() -> dict:
            multiworld = setup_multiworld([CVAOSWorld, CVAOSWorld], gen_steps, seed=31337)
            distribute_items_restrictive(multiworld)
            call_all(multiworld, "post_fill")
            return {(loc.player, loc.name): (loc.item.name, loc.item.player)
                    for loc in multiworld.get_filled_locations()}
        self.assertEqual(placement(), placement())


class RealmGateTest(unittest.TestCase):
    """The gate on the way to Chaos (room B14) needs all three Ancient Books."""

    def test_the_books_are_the_last_thing_the_gate_needs(self):
        multiworld = setup_multiworld(CVAOSWorld, BEFORE_FILL, seed=515,
                                      options={"goal": "chaos", "item_smoothing": "off"})
        world = multiworld.worlds[1]
        # Every item except the books, so every other part of the gate can be met.
        without_books = CollectionState(multiworld)
        for item in multiworld.itempool:
            if item.name not in ANCIENT_BOOK_NAMES:
                world.collect(without_books, item)
        without_books.sweep_for_advancements()
        self.assertFalse(can_reach_room(without_books, world, "B14"))
        with_books = without_books.copy()
        for item in multiworld.itempool:
            if item.name in ANCIENT_BOOK_NAMES:
                world.collect(with_books, item)
        with_books.sweep_for_advancements()
        self.assertTrue(can_reach_room(with_books, world, "B14"))


if __name__ == "__main__":
    unittest.main()
