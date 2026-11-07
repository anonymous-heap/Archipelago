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
import unittest
from argparse import Namespace

import Main
from BaseClasses import MultiWorld, PlandoOptions
from worlds.AutoWorld import AutoWorldRegister

from .. import CVAOSWorld

GAME = CVAOSWorld.game


def generate(seed: int, players: list[tuple[str, dict]], skip_balancing: bool = False) -> MultiWorld:
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
        (2, [(GAME, {"enemy_checks": "all", "item_smoothing_order": "spheres"})], "b344c1c3fcf61cdc"),
        (4, [(GAME, {"enemy_checks": "all"}), (GAME, {"item_smoothing": "loose"})], "a4b0d8ade57b654c"),
    )

    def test_seeds_with_wide_off_place_items_as_before(self):
        for seed, players, expected in self.BEFORE_WIDE:
            with self.subTest(seed=seed):
                self.assertEqual(placement_hash(generate(seed, players)), expected)


if __name__ == "__main__":
    unittest.main()
