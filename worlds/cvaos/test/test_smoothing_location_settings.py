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

from BaseClasses import CollectionState, LocationProgressType, MultiWorld
from Fill import distribute_items_restrictive
from test.general import setup_multiworld
from worlds.AutoWorld import call_all
from worlds.generic.Rules import exclusion_rules

from .. import CVAOSWorld
from ..item_smoothing import ANCIENT_BOOK_NAMES, BIASED_SOULS, BLACK_PANTHER
from ..locations import location_name_to_id

BEFORE_FILL = ("generate_early", "create_regions", "create_items", "set_rules",
               "connect_entrances", "generate_basic")
LOCATION_NAMES = sorted(location_name_to_id)
SMOOTHED = (*BIASED_SOULS, BLACK_PANTHER, *ANCIENT_BOOK_NAMES)


def generate(seed: int, options: dict, plando=None) -> MultiWorld:
    """Run the generation steps the way Main does, with exclusion rules and optional plando."""
    multiworld = setup_multiworld(CVAOSWorld, BEFORE_FILL, seed=seed,
                                  options={"item_smoothing": "strict", **options})
    world = multiworld.worlds[1]
    exclusion_rules(multiworld, 1, world.options.exclude_locations.value)
    if plando is not None:
        plando(multiworld, world)
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
            priority = set(random.Random(100 + seed).sample(LOCATION_NAMES, 10))
            multiworld = generate(seed, {"priority_locations": priority})
            with self.subTest(seed=seed):
                wrong = [loc.name for loc in multiworld.get_locations(1)
                         if loc.progress_type == LocationProgressType.PRIORITY
                         and not loc.item.advancement]
                self.assertEqual(wrong, [])

    def test_a_plandoed_ancient_book_stays_where_it_was_put(self):
        target = "Entrance Parapet [000 Lucky Charm]"

        def plando(multiworld: MultiWorld, world: CVAOSWorld) -> None:
            book = next(item for item in multiworld.itempool if item.name == "Ancient Book 3")
            multiworld.itempool.remove(book)
            world.get_location(target).place_locked_item(book)

        for seed in range(3):
            multiworld = generate(seed, {}, plando=plando)
            with self.subTest(seed=seed):
                self.assertEqual(multiworld.get_location(target, 1).item.name, "Ancient Book 3")

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


if __name__ == "__main__":
    unittest.main()
