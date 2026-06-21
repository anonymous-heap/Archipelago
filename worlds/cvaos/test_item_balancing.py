"""
Tests for the item-smoothing placement feature (see data/item_balancing/README.md):

* the Ancient Books are spread across early/mid/late by the pre_fill, and
* non-progression items are smoothed so less-desirable items land earlier and more-desirable
  ones later, with gold sprinkled early-to-mid -- all only when item_smoothing is enabled.
"""

import random
import unittest
from argparse import Namespace

from BaseClasses import CollectionState, MultiWorld
from Fill import distribute_items_restrictive
from test.bases import WorldTestBase
from test.general import gen_steps
from worlds.AutoWorld import AutoWorldRegister, call_all

from worlds.cvaos.data import item_balancing as desirability_data

GAME = "Castlevania - Aria of Sorrow"
from worlds.cvaos.item_smoothing import (
    ANCIENT_BOOK_NAMES,
    BIASED_SOULS,
    BLACK_PANTHER_EARLY_WINDOW,
    EARLY_BAND,
    _UNREACHED_DEPTH,
    _base_name,
    _category,
    _region_depth,
    _run_position,
    smoothable_locations_in_order,
)


def _run_fill(multiworld) -> None:
    """Run the main fill, then post_fill (which triggers stage_post_fill smoothing)."""
    distribute_items_restrictive(multiworld)
    call_all(multiworld, "post_fill")


def _ordered_with_desirability(multiworld, player, by_map_distance=False):
    """(rank_fraction, info) for each smoothed location's item that is ranked and not money,
    in progress order -- the achieved arrangement the feature is supposed to produce."""
    spheres = list(multiworld.get_spheres())
    depth = _region_depth(multiworld, player)
    ordered = smoothable_locations_in_order(multiworld, player, spheres, depth,
                                            by_map_distance=by_map_distance)
    n = max(1, len(ordered) - 1)
    out = []
    for rank, loc in enumerate(ordered):
        info = desirability_data.by_name.get(_base_name(loc.item.name))
        if info is not None and info.category != "money":
            out.append((rank / n, info))
    return ordered, out


class CVAOSWorldTestBase(WorldTestBase):
    game = "Castlevania - Aria of Sorrow"


class TestBooksSpread(CVAOSWorldTestBase):
    options = {"item_smoothing": "strict"}

    def test_books_placed_and_locked(self):
        self.world_setup(seed=123)
        multiworld = self.multiworld
        placed = {loc.item.name: loc
                  for loc in multiworld.get_locations(self.player)
                  if loc.item is not None and loc.item.name in ANCIENT_BOOK_NAMES}
        self.assertEqual(set(placed), set(ANCIENT_BOOK_NAMES),
                         "all three Ancient Books should be placed by pre_fill")
        for name, loc in placed.items():
            self.assertTrue(loc.locked, f"{name} should be locked in place")
            self.assertIsNotNone(loc.address)
        self.assertEqual([it for it in multiworld.itempool if it.name in ANCIENT_BOOK_NAMES], [],
                         "placed books must be removed from the itempool")

    def test_books_spread_over_spheres(self):
        # After the post-fill refine, the books sit at distinct points of the *run* (sphere ordering)
        # -- one early [0, 1/3], one mid [1/3, 2/3], one near 4/6 -- so positions span a wide range.
        spreads = []
        for seed in range(1000, 1008):
            self.world_setup(seed=seed)
            _run_fill(self.multiworld)  # distribute + post_fill -> runs refine_ancient_books
            position = _run_position(self.multiworld, self.player)
            book_fracs = [position[loc] for loc in self.multiworld.get_locations(self.player)
                          if loc.item is not None and loc.item.name in ANCIENT_BOOK_NAMES
                          and loc.item.player == self.player and loc in position]
            if len(book_fracs) == 3:
                spreads.append(max(book_fracs) - min(book_fracs))
        self.assertTrue(spreads, "no seed placed all three books")
        self.assertGreater(sum(spreads) / len(spreads), 0.4,
                           "books should span a wide range of the run by sphere ordering (~thirds)")


class TestSmoothingMinimalAccessibility(CVAOSWorldTestBase):
    """Smoothing must run cleanly under accessibility=minimal, where own filler can sit in unreachable
    spots -- the case smoothable_locations_in_order's unreachable-sphere sentinel guard handles."""
    options = {"item_smoothing": "strict", "accessibility": "minimal"}

    def test_runs_and_places_books(self):
        self.world_setup(seed=314)
        _run_fill(self.multiworld)
        books = [loc for loc in self.multiworld.get_locations(self.player)
                 if loc.item is not None and loc.item.name in ANCIENT_BOOK_NAMES]
        self.assertEqual(len(books), 3, "all three books placed under minimal accessibility")


class TestSmoothingStrict(CVAOSWorldTestBase):
    # Pinned to the spheres axis; the default is now map_distance (covered by TestSmoothingMapDistanceAxis).
    options = {"item_smoothing": "strict", "item_smoothing_order": "spheres"}

    def test_desirable_items_land_later(self):
        self.world_setup(seed=777)
        _run_fill(self.multiworld)
        _, pairs = _ordered_with_desirability(self.multiworld, self.player)
        self.assertGreater(len(pairs), 10, "need a meaningful sample of ranked items")
        pairs.sort(key=lambda p: p[1].desirability)
        half = len(pairs) // 2
        low_rank = sum(p[0] for p in pairs[:half]) / half
        high_rank = sum(p[0] for p in pairs[half:]) / (len(pairs) - half)
        self.assertGreater(high_rank, low_rank,
                           "more-desirable items should sit later in progress order on average")

    def test_gold_sprinkled_early_to_mid(self):
        self.world_setup(seed=778)
        _run_fill(self.multiworld)
        ordered, _ = _ordered_with_desirability(self.multiworld, self.player)
        n = max(1, len(ordered) - 1)
        gold_fracs = [rank / n for rank, loc in enumerate(ordered)
                      if _category(_base_name(loc.item.name)) == "money"]
        self.assertGreaterEqual(len(gold_fracs), 5, "expected several gold pickups")
        self.assertLess(sum(gold_fracs) / len(gold_fracs), 0.6,
                        "gold should center early-to-mid, not late")
        self.assertGreater(max(gold_fracs) - min(gold_fracs), 0.15,
                           "gold should be spread across a band, not clustered")


class TestSmoothingMapDistanceAxis(CVAOSWorldTestBase):
    """item_smoothing_order=map_distance smooths along structural map distance (seed-independent),
    instead of the default post-randomization spheres."""
    options = {"item_smoothing": "strict", "item_smoothing_order": "map_distance"}

    def test_ordering_is_monotonic_in_map_distance(self):
        self.world_setup(seed=4242)
        _run_fill(self.multiworld)
        depth = _region_depth(self.multiworld, self.player)
        spheres = list(self.multiworld.get_spheres())
        ordered = smoothable_locations_in_order(self.multiworld, self.player, spheres, depth,
                                                by_map_distance=True)
        self.assertGreater(len(ordered), 10, "need a meaningful number of smoothed locations")
        depths = [depth.get(loc.parent_region.name, _UNREACHED_DEPTH) for loc in ordered]
        self.assertEqual(depths, sorted(depths),
                         "map_distance ordering must be non-decreasing in structural map depth")

    def test_desirable_items_land_later_by_map_distance(self):
        self.world_setup(seed=4243)
        _run_fill(self.multiworld)
        _, pairs = _ordered_with_desirability(self.multiworld, self.player, by_map_distance=True)
        self.assertGreater(len(pairs), 10, "need a meaningful sample of ranked items")
        pairs.sort(key=lambda p: p[1].desirability)
        half = len(pairs) // 2
        low_rank = sum(p[0] for p in pairs[:half]) / half
        high_rank = sum(p[0] for p in pairs[half:]) / (len(pairs) - half)
        self.assertGreater(high_rank, low_rank,
                           "more-desirable items should sit later along map distance too")


class TestEarlyMobilityOff(CVAOSWorldTestBase):
    """early_mobility_souls=off skips the mobility-soul bias entirely: no targets are computed and
    the souls are left in the pool for the normal fill (rather than pre-placed)."""
    options = {"item_smoothing": "strict", "early_mobility_souls": "off",
               "black_panther_bias": False}

    def test_souls_not_biased(self):
        self.world_setup(seed=555)
        world = self.multiworld.worlds[self.player]
        self.assertIsNone(getattr(world, "_soul_targets", None),
                          "off must not compute soul lateness targets")
        pool_souls = {it.name for it in self.multiworld.itempool
                      if it.player == self.player and it.name in BIASED_SOULS}
        self.assertTrue(pool_souls,
                        "off must leave the mobility souls in the itempool, not pre-place them")


class TestEarlyMobilityGuaranteed(CVAOSWorldTestBase):
    """early_mobility_souls=guarantee_early always designates an early soul, so one of the three is
    always aimed at the early band (vs ~93% of seeds for the bias_early default)."""
    options = {"item_smoothing": "strict", "early_mobility_souls": "guarantee_early",
               "black_panther_bias": False}

    def test_one_soul_always_targeted_early(self):
        for seed in range(900, 906):
            self.world_setup(seed=seed)
            world = self.multiworld.worlds[self.player]
            targets = getattr(world, "_soul_targets", None)
            self.assertTrue(targets, f"seed {seed}: guarantee_early must store soul targets")
            self.assertLess(min(targets.values()), EARLY_BAND,
                            f"seed {seed}: guarantee_early must aim one soul at the early band")


class TestBlackPantherBiasOff(CVAOSWorldTestBase):
    """Black Panther Bias off leaves Black Panther to normal placement -- no early target."""
    options = {"item_smoothing": "strict", "black_panther_bias": False}

    def test_not_biased(self):
        self.world_setup(seed=606)
        world = self.multiworld.worlds[self.player]
        targets = getattr(world, "_soul_targets", None) or {}
        self.assertNotIn("Black Panther", targets,
                         "Black Panther Bias off must not give Black Panther an early target")


class TestBlackPantherBiasOn(CVAOSWorldTestBase):
    """Black Panther Bias on (default) targets Black Panther into the early window (by sphere ordering)
    in most seeds; every target that is set lands within that window."""
    options = {"item_smoothing": "strict"}

    def test_targets_early_in_most_seeds(self):
        n = 12
        targeted = 0
        for seed in range(700, 700 + n):
            self.world_setup(seed=seed)
            target = (getattr(self.multiworld.worlds[self.player], "_soul_targets", None) or {}).get("Black Panther")
            if target is not None:
                targeted += 1
                self.assertLessEqual(target, BLACK_PANTHER_EARLY_WINDOW,
                                     f"seed {seed}: a Black Panther target must sit in the early window")
        self.assertGreaterEqual(targeted, n // 2,
                                "with Black Panther Bias on, most seeds should target it early")


class TestSmoothingDeterminism(CVAOSWorldTestBase):
    options = {"item_smoothing": "strict"}

    def test_same_seed_same_placement(self):
        def run(seed):
            self.world_setup(seed=seed)
            _run_fill(self.multiworld)
            return {loc.name: (loc.item.name, loc.item.player)
                    for loc in self.multiworld.get_locations(self.player)
                    if loc.item is not None}
        self.assertEqual(run(2024), run(2024),
                         "smoothing must be deterministic for a fixed seed")


def _setup_filled_multiworld(players, seed, options):
    """Build and fully fill (incl. stage_post_fill) a multiworld of N cvaos slots, deterministically."""
    multiworld = MultiWorld(players)
    multiworld.player_name = {p: f"P{p}" for p in range(1, players + 1)}
    for player in range(1, players + 1):
        multiworld.game[player] = GAME
    multiworld.set_seed(seed)
    random.seed(multiworld.seed)
    args = Namespace()
    for name, option in AutoWorldRegister.world_types[GAME].options_dataclass.type_hints.items():
        value = options.get(name, option.default)
        setattr(args, name, {p: option.from_any(value) for p in range(1, players + 1)})
    multiworld.set_options(args)
    multiworld.state = CollectionState(multiworld)
    for step in gen_steps:
        call_all(multiworld, step)
    distribute_items_restrictive(multiworld)
    call_all(multiworld, "post_fill")
    return multiworld


class TestMultiworldDeterminism(unittest.TestCase):
    """The smoothing is scoped to on-world locations, so two cvaos slots stay deterministic for a
    fixed seed (this is the regression guard for the cross-player set-ordering hazard)."""

    def test_two_cvaos_slots_same_seed_same_placement(self):
        def run():
            multiworld = _setup_filled_multiworld(2, 31337, {"item_smoothing": "strict"})
            return {(loc.player, loc.name): (loc.item.name, loc.item.player)
                    for loc in multiworld.get_locations() if loc.item is not None}
        self.assertEqual(run(), run())


class TestSmoothingOrderWiring(unittest.TestCase):
    """item_smoothing_order is actually threaded into the post-fill smoothing pass: flipping it on a
    fixed seed changes the resulting arrangement (guards against the option being silently ignored)."""

    def test_axis_changes_arrangement(self):
        def arrangement(order_key):
            multiworld = _setup_filled_multiworld(
                1, 9001, {"item_smoothing": "strict", "item_smoothing_order": order_key})
            return {loc.name: loc.item.name for loc in multiworld.get_locations(1)
                    if loc.item is not None}
        self.assertNotEqual(arrangement("spheres"), arrangement("map_distance"),
                            "flipping item_smoothing_order must change the smoothing arrangement")


class TestBooksAsRequiredProgression(unittest.TestCase):
    """Forward-looking: once the Ancient Books are required to reach Graham for the good ending (a real
    item_requirements access rule), the book smoothing must still yield a beatable seed with the books
    spread. Simulate by wrapping the completion condition to require all three books, then run the
    pre-fill spread + main fill + post-fill refine and confirm accessibility still holds."""

    def test_smoothing_safe_when_books_required(self):
        for seed in range(2000, 2004):
            multiworld = MultiWorld(1)
            multiworld.player_name = {1: "P1"}
            multiworld.game[1] = GAME
            multiworld.set_seed(seed)
            random.seed(multiworld.seed)
            args = Namespace()
            for name, option in AutoWorldRegister.world_types[GAME].options_dataclass.type_hints.items():
                setattr(args, name, {1: option.from_any({"item_smoothing": "strict"}.get(name, option.default))})
            multiworld.set_options(args)
            multiworld.state = CollectionState(multiworld)
            for step in gen_steps:
                call_all(multiworld, step)
            # completion_condition was set in create_regions; now also require the three books, as the
            # good-ending Graham gate eventually will. The post-fill refine's accessibility check must
            # respect this (and the pre-fill spread must not have locked a book where it's needed).
            base = multiworld.completion_condition[1]
            multiworld.completion_condition[1] = (
                lambda state, base=base: base(state) and all(state.has(b, 1) for b in ANCIENT_BOOK_NAMES))
            distribute_items_restrictive(multiworld)
            call_all(multiworld, "post_fill")
            self.assertTrue(multiworld.fulfills_accessibility(),
                            f"seed {seed}: seed must stay beatable when the books are required progression")
            position = _run_position(multiworld, 1)
            fracs = sorted(position[loc] for loc in multiworld.get_locations(1)
                           if loc.item is not None and loc.item.name in ANCIENT_BOOK_NAMES and loc in position)
            self.assertEqual(len(fracs), 3, f"seed {seed}: all three books placed")
            self.assertGreater(fracs[-1] - fracs[0], 0.25,
                               f"seed {seed}: books should still be spread when required")


class TestRealmGateRequiresBooks(unittest.TestCase):
    """The Chaotic Realm gate (good ending) now requires all three Ancient Books. Verify the gate
    actually enforces it (B14 unreachable without the books, reachable with them) and that a full
    chaos-goal generation with smoothing on stays beatable with the books as real gating progression."""

    @staticmethod
    def _build(seed, options):
        multiworld = MultiWorld(1)
        multiworld.player_name = {1: "P1"}
        multiworld.game[1] = GAME
        multiworld.set_seed(seed)
        random.seed(multiworld.seed)
        args = Namespace()
        for name, option in AutoWorldRegister.world_types[GAME].options_dataclass.type_hints.items():
            setattr(args, name, {1: option.from_any(options.get(name, option.default))})
        multiworld.set_options(args)
        multiworld.state = CollectionState(multiworld)
        return multiworld

    def test_gate_requires_books(self):
        from worlds.cvaos.regions import can_reach_room, ANCIENT_BOOKS
        multiworld = self._build(515, {"goal": "chaos", "item_smoothing": "off"})
        for step in ("create_regions", "create_items", "set_rules", "generate_basic"):
            call_all(multiworld, step)
        world = multiworld.worlds[1]
        # Collect every itempool item EXCEPT the three books -> all other gate terms satisfiable.
        without_books = CollectionState(multiworld)
        for item in multiworld.itempool:
            if item.name not in ANCIENT_BOOKS:
                multiworld.worlds[item.player].collect(without_books, item)
        without_books.sweep_for_advancements()
        self.assertFalse(can_reach_room(without_books, world, "B14"),
                         "Chaos arena must be UNREACHABLE without all three Ancient Books")
        # Now add the books -> the gate opens (proves books were the only missing term).
        with_books = without_books.copy()
        for name in ANCIENT_BOOKS:
            world.collect(with_books, next(it for it in multiworld.itempool if it.name == name))
        with_books.sweep_for_advancements()
        self.assertTrue(can_reach_room(with_books, world, "B14"),
                        "Chaos arena must be reachable once all three Ancient Books are in hand")

    def test_chaos_seed_with_smoothing_is_beatable(self):
        for seed in range(2100, 2104):
            multiworld = self._build(seed, {"goal": "chaos", "item_smoothing": "strict"})
            for step in gen_steps:
                call_all(multiworld, step)
            distribute_items_restrictive(multiworld)
            call_all(multiworld, "post_fill")
            self.assertTrue(multiworld.fulfills_accessibility(),
                            f"seed {seed}: chaos-goal seed with smoothing must be beatable (books gate the realm)")


class TestSmoothingOff(CVAOSWorldTestBase):
    options = {"item_smoothing": "off"}

    def test_off_is_inert(self):
        self.world_setup(seed=42)
        multiworld = self.multiworld
        # pre_fill is a no-op: the three books are still loose in the pool, unplaced.
        self.assertEqual(len([it for it in multiworld.itempool if it.name in ANCIENT_BOOK_NAMES]), 3,
                         "with smoothing off, books must not be pre-placed")
        _run_fill(multiworld)
        book_locs = [loc for loc in multiworld.get_locations(self.player)
                     if loc.item is not None and loc.item.name in ANCIENT_BOOK_NAMES]
        self.assertEqual(len(book_locs), 3, "books should still be placed by the normal fill")
