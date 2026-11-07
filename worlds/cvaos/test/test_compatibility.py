"""
Tests that an outdated install refuses a seed it can't run, and that released seeds still work.

A seed's identifier is the newest among the identifiers that introduced its rom_config.json keys,
and never older than V0.5, which released clients refuse. The client asks the server for the
player's own items under every identifier after the released V0.3 and V0.4, and slot data always
lists the pickups whose items the ROM hands out.

    python -m pytest worlds/cvaos/test/test_compatibility.py -v
"""
from __future__ import annotations

import unittest
from unittest import mock

from test.general import setup_multiworld

from .. import CVAOSWorld
from ..client_logic import ITEMS_FROM_OTHER_WORLDS, ITEMS_INCLUDING_OWN, items_handling_for
from ..locations import pickup_location_ids
from ..rom import patch
from .test_smoothing_location_settings import generate


class IdentifierTest(unittest.TestCase):
    def test_a_seed_carries_at_least_the_minimum_identifier(self):
        world = setup_multiworld(CVAOSWorld, ("generate_early",), seed=1).worlds[1]
        config = patch.build_rom_config(world)
        self.assertEqual(patch.identifier_for(config), patch.MIN_SEED_IDENTIFIER)
        self.assertIn(patch.identifier_for(config), patch.COMPATIBLE_IDENTIFIERS)

    def test_a_newer_key_raises_the_identifier(self):
        newer = "CVAOS_AP_V0.9"
        with mock.patch.object(patch, "IDENTIFIERS", (*patch.IDENTIFIERS, newer)), \
                mock.patch.dict(patch.ROM_CONFIG_KEY_IDENTIFIERS, {"newer_feature": newer}):
            self.assertEqual(patch.identifier_for({"skull_key_warp": True, "newer_feature": 1}),
                             newer)

    def test_the_client_asks_for_own_items_after_the_released_identifiers(self):
        for identifier in sorted(patch.COMPATIBLE_IDENTIFIERS | {"CVAOS_AP_V0.9"}):
            expected = (ITEMS_FROM_OTHER_WORLDS if identifier in patch.RELEASED_IDENTIFIERS
                        else ITEMS_INCLUDING_OWN)
            with self.subTest(identifier=identifier):
                self.assertEqual(items_handling_for(identifier.encode("ascii")), expected)
        self.assertEqual(patch.RELEASED_IDENTIFIERS, {"CVAOS_AP_V0.3", "CVAOS_AP_V0.4"})


class SlotDataTest(unittest.TestCase):
    def test_slot_data_lists_the_rom_given_pickups_without_enemy_checks(self):
        multiworld = generate(2, {"enemy_checks": "off"})
        world = multiworld.worlds[1]
        listed = set(world.fill_slot_data()["rom_given_locations"])
        expected = {location.address for location in multiworld.get_locations(1)
                    if location.address in pickup_location_ids
                    and patch.rom_gives_item(world, location)}
        self.assertTrue(listed)
        self.assertEqual(listed, expected)


if __name__ == "__main__":
    unittest.main()
