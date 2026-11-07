"""
Tests for Wide Item Smoothing (wide_smoothing.py).

The seeds here go through Main.main, so locality, exclusion, plando and core's own progression
balancing all take effect. The tests assert the mechanisms rather than rates. By default they
generate a duo of two Aria of Sorrow slots. With CVAOS_WIDE_SCENARIOS=1 they also generate 30 solo
preset seeds and 10 seeds each of a Hollow Knight duo, a Dark Souls III duo and a duo whose Aria of
Sorrow slot keeps weapons local and armor non-local.

    python -m pytest worlds/cvaos/test/test_wide_smoothing.py -v
"""
from __future__ import annotations

import hashlib
import os
import unittest
from argparse import Namespace
from unittest import mock

import Main
from BaseClasses import ItemClassification, MultiWorld, PlandoOptions
from test.general import setup_multiworld
from worlds.AutoWorld import AutoWorldRegister

from .. import CVAOSWorld, wide_smoothing
from ..data.item_balancing import worth
from ..items import logic_only_progression_names
from ..locations import CVAOSLocation
from ..wide_smoothing import BAND, OrderResult, _is_defeat, promote_mid_game_gear

GAME = CVAOSWorld.game
PRESET = {"enemy_checks": "all", "soul_shuffle": "matched", "enemy_drop_rate": 350,
          "wide_item_smoothing": True}
SCENARIOS = bool(os.environ.get("CVAOS_WIDE_SCENARIOS"))


def generate(seed: int, players: list[tuple[str, dict]],
             skip_balancing: bool = False) -> MultiWorld:
    """Generate a multiworld through Main.main, without output, and return it.

    Each player is a game name and that player's options. Options left out take their defaults.
    """
    args = Namespace(multi=len(players), race=False, outputname=None, outputpath=None, spoiler=0,
                     plando=PlandoOptions.from_set({"items"}), csv_output=False, skip_output=True,
                     spoiler_only=False, skip_prog_balancing=skip_balancing,
                     game={}, name={}, sprite={}, sprite_pool={})
    for player, (game, options) in enumerate(players, 1):
        args.game[player] = game
        args.name[player] = f"{game.split()[0]}{player}"
        args.sprite[player] = None
        args.sprite_pool[player] = []
        for key, option in AutoWorldRegister.world_types[game].options_dataclass.type_hints.items():
            if not hasattr(args, key):
                setattr(args, key, {})
            value = option.from_any(options.get(key, option.default))
            value.verify(AutoWorldRegister.world_types[game], args.name[player], args.plando)
            getattr(args, key)[player] = value
    return Main.main(args, seed)


def placement_hash(multiworld: MultiWorld) -> str:
    rows = sorted(f"{loc.player}|{loc.name}|{loc.item.player}|{loc.item.name}"
                  for loc in multiworld.get_filled_locations())
    return hashlib.sha1("\n".join(rows).encode("utf-8")).hexdigest()[:16]


class WideOffTest(unittest.TestCase):
    # Placement hashes from the world as it was before Wide Item Smoothing existed. With the option
    # off, the same seed and options place every item as they did then.
    BEFORE_WIDE = (
        (2, [(GAME, {"enemy_checks": "all", "item_smoothing_order": "spheres"})],
         "b344c1c3fcf61cdc"),
        (4, [(GAME, {"enemy_checks": "all"}), (GAME, {"item_smoothing": "loose"})],
         "a4b0d8ade57b654c"),
    )

    def test_seeds_with_wide_off_place_items_as_before(self):
        for seed, players, expected in self.BEFORE_WIDE:
            with self.subTest(seed=seed):
                self.assertEqual(placement_hash(generate(seed, players)), expected)


class PoolTest(unittest.TestCase):
    def test_mid_game_filler_gear_becomes_useful(self):
        world = generate(1, [(GAME, {})]).worlds[1]
        pool = [world.create_item(name) for name in ("Samurai Armor", "Gungner", "Copper Plate",
                                                     "Potion")]
        promote_mid_game_gear(pool)
        self.assertEqual([item.classification for item in pool],
                         [ItemClassification.useful, ItemClassification.useful,
                          world.create_item("Copper Plate").classification,
                          ItemClassification.filler])


class TradeTest(unittest.TestCase):
    """The order's step for a location that accepts no remaining item."""

    def deal(self, accepted: list[set[str]]) -> tuple[bool, list[CVAOSLocation], OrderResult]:
        """Deal a Potion and a High Potion to locations that accept only the named items."""
        multiworld = setup_multiworld(CVAOSWorld, (), seed=1)
        locations = []
        for index, names in enumerate(accepted):
            location = CVAOSLocation(1, f"Test {index}")
            location.item_rule = lambda item, names=names: item.name in names
            locations.append(location)
        positions = {location: 0.1 * index for index, location in enumerate(locations, 1)}
        pool = [multiworld.worlds[1].create_item(name) for name in ("Potion", "High Potion")]
        result = OrderResult(positions)
        dealt = wide_smoothing._deal(locations, pool, positions, multiworld.state, result)
        return dealt, locations, result

    def test_a_location_that_fits_no_remaining_item_trades_with_an_earlier_one(self):
        dealt, locations, result = self.deal([{"Potion", "High Potion"}, {"Potion"}])
        self.assertTrue(dealt)
        self.assertEqual([location.item.name for location in locations], ["High Potion", "Potion"])
        self.assertEqual(result.trades, 1)

    def test_the_deal_fails_when_no_pair_of_locations_accepts_the_trade(self):
        dealt, _locations, _result = self.deal([{"Potion"}, {"Potion"}])
        self.assertFalse(dealt)

    def test_an_order_that_fails_to_deal_is_undone(self):
        multiworld = generate(3, [(GAME, PRESET)])
        before = placement_hash(multiworld)

        def scramble_and_fail(locations, pool, *_args):
            for location, item in zip(locations, reversed(pool)):
                location.item = item
                item.location = location
            return False

        with mock.patch.object(wide_smoothing, "_deal", side_effect=scramble_and_fail):
            result = wide_smoothing._order_player(
                multiworld, multiworld.worlds[1], wide_smoothing.walk(multiworld, room_clock=True),
                wide_smoothing.walk(multiworld, room_clock=False))
        self.assertTrue(result.undone)
        self.assertEqual(placement_hash(multiworld), before)


class WideScenario:
    """The mechanisms every scenario asserts. A subclass names its players and seeds."""

    PLAYERS: list[tuple[str, dict]] = []
    SEEDS: tuple[int, ...] = ()
    runs: list[MultiWorld]

    @classmethod
    def setUpClass(cls) -> None:
        cls.runs = [generate(seed, cls.PLAYERS) for seed in cls.SEEDS]

    def wide_worlds(self):
        for multiworld in self.runs:
            for world in multiworld.get_game_worlds(GAME):
                if world.options.wide_item_smoothing:
                    yield multiworld, world

    def test_every_unlocked_location_accepts_its_item(self):
        # Wide moves only its own players' items. Another world's own smoothing, such as Dark Souls
        # III's, may leave that world's items where its own rules reject them.
        for multiworld in self.runs:
            moved = {world.player for _multiworld, world in self.wide_worlds()
                     if _multiworld is multiworld}
            broken = [f"{loc.player}:{loc.name} <- {loc.item.name}"
                      for loc in multiworld.get_filled_locations()
                      if loc.item.player in moved and not loc.locked
                      and not loc.can_fill(multiworld.state, loc.item, False)]
            self.assertEqual(broken, [], multiworld.seed)

    def test_equal_seeds_give_equal_placements(self):
        self.assertEqual(placement_hash(generate(self.SEEDS[0], self.PLAYERS)),
                         placement_hash(self.runs[0]))

    def test_pinned_locations_hold_breakers(self):
        for multiworld, world in self.wide_worlds():
            for order in world.wide_report.orders:
                pinned = [order.placed[loc].name for loc in order.pinned]
                self.assertTrue(all(name in logic_only_progression_names for name in pinned),
                                (multiworld.seed, pinned))

    def test_the_restore_never_fires(self):
        for multiworld, world in self.wide_worlds():
            self.assertEqual(world.wide_report.restores, 0, multiworld.seed)

    def test_nothing_sits_above_the_band_except_counted_placements(self):
        for multiworld, world in self.wide_worlds():
            for order in world.wide_report.orders:
                above = [loc.name for loc, item in order.placed.items()
                         if worth.item_worth(item.name) > order.positions[loc] + BAND]
                self.assertLessEqual(len(above), order.over_band, (multiworld.seed, above))


class WideDuoTest(WideScenario, unittest.TestCase):
    PLAYERS = [(GAME, PRESET), (GAME, PRESET)]
    SEEDS = (1,)

    def test_the_order_covers_pickups_defeats_and_items_abroad(self):
        for _multiworld, world in self.wide_worlds():
            dealt = world.wide_report.orders[-1].positions
            self.assertTrue(any(loc.player == world.player and _is_defeat(loc) for loc in dealt))
            self.assertTrue(any(loc.player == world.player and not _is_defeat(loc)
                                for loc in dealt))
            self.assertTrue(any(loc.player != world.player for loc in dealt))

    def test_the_pin_keeps_a_breaker_where_logic_first_finds_one(self):
        self.assertTrue(any(order.pinned for _multiworld, world in self.wide_worlds()
                            for order in world.wide_report.orders))


def scenario(cls):
    return unittest.skipUnless(SCENARIOS, "set CVAOS_WIDE_SCENARIOS=1 to run")(cls)


@scenario
class SoloPresetTest(WideScenario, unittest.TestCase):
    PLAYERS = [(GAME, PRESET)]
    SEEDS = tuple(range(100, 130))


@scenario
class HollowKnightDuoTest(WideScenario, unittest.TestCase):
    PLAYERS = [(GAME, PRESET), ("Hollow Knight", {})]
    SEEDS = tuple(range(200, 210))


@scenario
class DarkSoulsDuoTest(WideScenario, unittest.TestCase):
    PLAYERS = [(GAME, PRESET), ("Dark Souls III", {})]
    SEEDS = tuple(range(300, 310))


@scenario
class LocalityDuoTest(WideScenario, unittest.TestCase):
    PLAYERS = [(GAME, {**PRESET, "local_items": ["Weapons"], "non_local_items": ["Armor"]}),
               ("Hollow Knight", {})]
    SEEDS = tuple(range(400, 410))


if __name__ == "__main__":
    unittest.main()
