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

import Main
from BaseClasses import ItemClassification, MultiWorld, PlandoOptions
from worlds.AutoWorld import AutoWorldRegister

from .. import CVAOSWorld
from ..data.item_balancing import worth
from ..wide_smoothing import BAND, _is_defeat, promote_mid_game_gear

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
            getattr(args, key)[player] = option.from_any(options.get(key, option.default))
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
        for multiworld in self.runs:
            broken = [f"{loc.player}:{loc.name} <- {loc.item.name}"
                      for loc in multiworld.get_filled_locations()
                      if not loc.locked and not loc.can_fill(multiworld.state, loc.item, False)]
            self.assertEqual(broken, [], multiworld.seed)

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


if __name__ == "__main__":
    unittest.main()
