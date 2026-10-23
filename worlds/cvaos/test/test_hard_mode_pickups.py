"""
The Hard Mode Pickups option: which seeds shuffle the five Hard Mode pickups, and how the patch
lets them spawn outside Hard Mode.

    python -m pytest worlds/cvaos/test/test_hard_mode_pickups.py -v
"""
from __future__ import annotations

import unittest
from collections import Counter

from BaseClasses import MultiWorld
from Fill import distribute_items_restrictive
from test.general import gen_steps, setup_multiworld

from .. import CVAOSWorld
from ..data import pickup_info_collection
from ..options import HardModePickups
from ..rom.entity import ENTITY_SIZE, GBA_ROM_BASE, HARD_PICKUP, PICKUP
from ..rom.patch import _TYPE_OFF, get_location_data

STEPS = ("generate_early", "create_regions", "create_items")
HARD_PICKUPS = [p for p in pickup_info_collection if p.is_hard_mode_only]
HARD_LOCATIONS = {p.location_name for p in HARD_PICKUPS}
HARD_ITEMS = Counter(p.display_name for p in HARD_PICKUPS)

# (hard_mode, hard_mode_pickups) -> whether the five pickups are shuffled.
EXPECTED = {
    (False, "with_hard_mode"): False,
    (True, "with_hard_mode"): True,
    (False, "always"): True,
    (True, "always"): True,
    (False, "never"): False,
    (True, "never"): False,
}


def build(hard_mode: bool, hard_mode_pickups: str | None, steps=STEPS, seed: int = 3) -> MultiWorld:
    options = {"hard_mode": hard_mode}
    if hard_mode_pickups is not None:
        options["hard_mode_pickups"] = hard_mode_pickups
    return setup_multiworld(CVAOSWorld, steps, seed=seed, options=options)


def location_writes(multiworld: MultiWorld) -> tuple[list, dict]:
    """Every filled location, and the writes the patch builds for them."""
    locations = [loc for loc in multiworld.get_locations(1) if loc.address is not None]
    return locations, get_location_data(multiworld.worlds[1], locations)


def written_types(multiworld: MultiWorld) -> list[int]:
    """The entity type byte that the patch writes at every filled location."""
    locations, writes = location_writes(multiworld)
    return [writes[loc.address - GBA_ROM_BASE + _TYPE_OFF][0] for loc in locations]


class InclusionTest(unittest.TestCase):
    def test_there_are_five_hard_mode_pickups(self):
        self.assertEqual(len(HARD_PICKUPS), 5)

    def test_with_hard_mode_is_the_default(self):
        self.assertEqual(HardModePickups.default, HardModePickups.option_with_hard_mode)

    def test_locations_and_items_follow_the_option(self):
        for (hard_mode, setting), included in EXPECTED.items():
            with self.subTest(hard_mode=hard_mode, hard_mode_pickups=setting):
                multiworld = build(hard_mode, setting)
                names = {loc.name for loc in multiworld.get_locations(1)}
                items = [*multiworld.itempool, *multiworld.worlds[1].get_pre_fill_items()]
                pool = Counter(item.name for item in items)
                self.assertEqual(HARD_LOCATIONS & names, HARD_LOCATIONS if included else set())
                self.assertEqual(pool & HARD_ITEMS, HARD_ITEMS if included else Counter())
                self.assertEqual(len(items), len(names))

    def test_leaving_the_option_unset_behaves_like_with_hard_mode(self):
        for hard_mode in (False, True):
            with self.subTest(hard_mode=hard_mode):
                unset = {loc.name for loc in build(hard_mode, None).get_locations(1)}
                explicit = {loc.name for loc in build(hard_mode, "with_hard_mode").get_locations(1)}
                self.assertEqual(unset, explicit)


class SpawnTypeTest(unittest.TestCase):
    """A Hard Mode item makes its location a HARD_PICKUP, unless Hard Mode Pickups is ``always``.
    The type follows the item, so the item is placed at an ordinary location, which is where a
    real fill nearly always puts it."""

    def _written_type(self, hard_mode: bool, setting: str) -> int:
        world = build(hard_mode, setting).worlds[1]
        location = next(loc for loc in world.get_locations() if loc.name not in HARD_LOCATIONS)
        location.place_locked_item(world.create_item(HARD_PICKUPS[0].display_name))
        writes = get_location_data(world, [location])
        return writes[location.address - GBA_ROM_BASE + _TYPE_OFF][0]

    def test_always_writes_an_ordinary_pickup(self):
        self.assertEqual(self._written_type(False, "always"), PICKUP)
        self.assertEqual(self._written_type(True, "always"), PICKUP)

    def test_with_hard_mode_keeps_the_hard_pickup_type(self):
        self.assertEqual(self._written_type(True, "with_hard_mode"), HARD_PICKUP)


class FilledSeedTest(unittest.TestCase):
    """End to end: fill a seed, then build the location writes for every location."""

    def _filled(self, hard_mode: bool, setting: str, seed: int) -> MultiWorld:
        multiworld = build(hard_mode, setting, steps=gen_steps, seed=seed)
        distribute_items_restrictive(multiworld)
        return multiworld

    def test_always_leaves_no_hard_pickup_anywhere(self):
        for hard_mode in (False, True):
            for seed in (1, 2):
                with self.subTest(hard_mode=hard_mode, seed=seed):
                    self.assertNotIn(HARD_PICKUP, written_types(self._filled(hard_mode, "always", seed)))

    def test_with_hard_mode_writes_one_hard_pickup_per_hard_item(self):
        types = written_types(self._filled(True, "with_hard_mode", 1))
        self.assertEqual(types.count(HARD_PICKUP), len(HARD_PICKUPS))

    def test_never_leaves_the_vanilla_hard_pickups_untouched(self):
        _locations, writes = location_writes(self._filled(True, "never", 1))
        for pickup in HARD_PICKUPS:
            record = range(pickup.ptr_address - GBA_ROM_BASE,
                           pickup.ptr_address - GBA_ROM_BASE + ENTITY_SIZE)
            with self.subTest(pickup=pickup.display_name):
                self.assertFalse(any(offset in record for offset in writes))


if __name__ == "__main__":
    unittest.main()
