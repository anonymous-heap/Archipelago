"""
Tests that an outdated install refuses a seed it can't run, and that released seeds still work.

A seed's identifier is the newest among the identifiers that introduced its rom_config.json keys,
and never older than V0.5, which released clients refuse. The client asks the server for the
player's own items under every identifier after the released V0.3 and V0.4, and slot data always
lists the pickups whose items the ROM hands out.

Every seed's patch lists the apply_enemy_table step, which an apworld from before V0.5 does not
know, and the patcher refuses a rom_config.json key it does not know. A released patch, which
lacks the step, still applies.

    python -m pytest worlds/cvaos/test/test_compatibility.py -v
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from test.general import setup_multiworld
from worlds.Files import APTokenTypes

from .. import CVAOSWorld
from ..client_logic import ITEMS_FROM_OTHER_WORLDS, ITEMS_INCLUDING_OWN, items_handling_for
from ..locations import pickup_location_ids
from ..rom import identifiers, patch
from . import find_rom, generate, run_apply_rom_features

# The procedure and rom_config.json of a patch from the released V0.4 world, with its defaults.
RELEASED_PROCEDURE = [("apply_tokens", ["token_data.bin"]),
                      ("apply_rom_features", ["rom_config.json"])]
RELEASED_CONFIG = {
    "target_platform": 0, "skull_key_warp": True, "ancient_book_soul_drops": True,
    "forbidden_area_pickup": True, "single_jump_divekick": True, "classicvania_movement": False,
    "oops_all_whips": False, "soul_shuffle_plan": {}, "keep_soul_drop_rates": True,
    "shuffle_starting_soul": True, "multiply_soul_drop_rates": False,
    "soul_drop_rate_multiplier": 120,
}


class IdentifierTest(unittest.TestCase):
    def test_a_seed_carries_at_least_the_minimum_identifier(self):
        world = setup_multiworld(CVAOSWorld, ("generate_early",), seed=1).worlds[1]
        config = patch.build_rom_config(world)
        self.assertEqual(identifiers.identifier_for(config), identifiers.MIN_SEED_IDENTIFIER)
        self.assertIn(identifiers.identifier_for(config), identifiers.COMPATIBLE_IDENTIFIERS)

    def test_a_newer_key_raises_the_identifier(self):
        newer = "CVAOS_AP_V0.9"
        with mock.patch.object(identifiers, "IDENTIFIERS", (*identifiers.IDENTIFIERS, newer)), \
                mock.patch.dict(identifiers.ROM_CONFIG_KEY_IDENTIFIERS, {"newer_feature": newer}):
            config = {"skull_key_warp": True, "newer_feature": 1}
            self.assertEqual(identifiers.identifier_for(config), newer)

    def test_the_client_asks_for_own_items_after_the_released_identifiers(self):
        for identifier in sorted(identifiers.COMPATIBLE_IDENTIFIERS | {"CVAOS_AP_V0.9"}):
            expected = (ITEMS_FROM_OTHER_WORLDS if identifier in identifiers.RELEASED_IDENTIFIERS
                        else ITEMS_INCLUDING_OWN)
            with self.subTest(identifier=identifier):
                self.assertEqual(items_handling_for(identifier.encode("ascii")), expected)
        self.assertEqual(identifiers.RELEASED_IDENTIFIERS, {"CVAOS_AP_V0.3", "CVAOS_AP_V0.4"})


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



class PatchProcedureTest(unittest.TestCase):
    def test_every_seed_lists_the_enemy_table_step_after_the_released_steps(self):
        manifest = patch.CVAOSProcedurePatch(player=1, player_name="Soma").get_manifest()
        self.assertEqual(manifest["procedure"],
                         [*RELEASED_PROCEDURE, ("apply_enemy_table", ["rom_config.json"])])

    def test_an_unknown_key_raises_and_names_the_seeds_identifier(self):
        rom = bytearray(0x800000)
        start = identifiers.ARCHIPELAGO_IDENTIFIER_START
        rom[start:start + 13] = b"CVAOS_AP_V0.9"
        with self.assertRaisesRegex(ValueError, "newer_feature.*CVAOS_AP_V0.9"):
            run_apply_rom_features(self, bytes(rom), newer_feature=[])
        # Without an identifier in the ROM, the message still names the key.
        with self.assertRaisesRegex(ValueError, "newer_feature"):
            run_apply_rom_features(self, bytes(0x800000), newer_feature=[])

    def test_every_key_a_seed_writes_is_known(self):
        world = setup_multiworld(CVAOSWorld, ("generate_early",), seed=1).worlds[1]
        self.assertLessEqual(set(patch.build_rom_config(world)),
                             set(identifiers.ROM_CONFIG_KEY_IDENTIFIERS))
        self.assertLessEqual(set(RELEASED_CONFIG), set(identifiers.ROM_CONFIG_KEY_IDENTIFIERS))

    def test_a_released_patch_without_the_step_still_applies(self):
        rom_path = find_rom()
        if rom_path is None:
            self.skipTest("needs a USA Aria of Sorrow ROM")
        with open(rom_path, "rb") as fh:
            base = fh.read()
        released = patch.CVAOSProcedurePatch(player=1, player_name="Soma")
        released.procedure = RELEASED_PROCEDURE
        released.write_token(APTokenTypes.WRITE, identifiers.ARCHIPELAGO_IDENTIFIER_START,
                             b"CVAOS_AP_V0.4")
        released.write_file("rom_config.json", json.dumps(RELEASED_CONFIG).encode("utf-8"))
        released.write_file("token_data.bin", released.get_token_binary())
        with tempfile.TemporaryDirectory() as folder:
            patch_path = os.path.join(folder, "released.apcvaos")
            out_path = os.path.join(folder, "released.gba")
            released.write(patch_path)
            reread = patch.CVAOSProcedurePatch(patch_path)
            with patch.base_rom_override(base), mock.patch.object(
                    patch.CVAOSPatchExtension, "apply_enemy_table") as step:
                reread.patch(out_path)
            with open(out_path, "rb") as fh:
                result = fh.read()
        step.assert_not_called()
        start = identifiers.ARCHIPELAGO_IDENTIFIER_START
        self.assertEqual(result[start:start + 13], b"CVAOS_AP_V0.4")
        self.assertNotEqual(result, base)


if __name__ == "__main__":
    unittest.main()
