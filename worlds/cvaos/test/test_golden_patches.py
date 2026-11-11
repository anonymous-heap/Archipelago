"""
Golden patch tests: the patches that this release generated must keep building the same ROMs.

The fixtures in ``data/golden`` are ``.apcvaos`` files from the first release with the Experimental
options. Between them they cover the patch's features, and ``golden_patches.FIXTURES`` says what
each one covers. Beside each patch, a JSON file holds the SHA-256 of the ROM it builds from the
cart dump and from the Advance Collection's copy. These tests apply each patch the way a player's
install does and compare the hash, so a change that alters a released seed's ROM fails here. A
base ROM that the test helpers can't find is skipped.

A failure names the fixture, the base ROM and the first differing file offset. When the change is
intended, record the new values from the Archipelago folder with

    python -m worlds.cvaos.test.golden_patches

and say in the commit which seeds now patch differently, and why. Recording needs both base ROMs,
and it leaves the fixtures as they are. ``--new-fixtures`` generates them again as well, which is
right only when a new release becomes the baseline.

The other tests need no ROM. They check that this version's patch extension can still load every
fixture: each procedure step exists, and each ``rom_config.json`` key is one it knows.

    python -m pytest worlds/cvaos/test/test_golden_patches.py -v
"""
from __future__ import annotations

import hashlib
import json
import os
import unittest

from worlds.Files import AutoPatchExtensionRegister

from ..rom import identifiers, patch
from . import base_roms
from .golden_patches import (BASES, FIXTURES, FOLDER, apply_fixture, first_difference,
                             load_expected, patch_path)


def _read(name: str) -> patch.CVAOSProcedurePatch:
    container = patch.CVAOSProcedurePatch(patch_path(name))
    container.read()
    return container


class GoldenPatchTest(unittest.TestCase):
    roms: dict[str, bytes]

    @classmethod
    def setUpClass(cls) -> None:
        cls.roms = base_roms()

    def check_base(self, base_name: str) -> None:
        base = self.roms.get(base_name)
        if base is None:
            self.skipTest(f"needs the {base_name} ROM")
        for name in FIXTURES:
            with self.subTest(fixture=name):
                expected = load_expected(name)[base_name]
                rom = apply_fixture(name, base)
                digest = hashlib.sha256(rom).hexdigest()
                if digest != expected["sha256"]:
                    self.fail(
                        f"The {name} fixture builds a different ROM from the {base_name} ROM. "
                        f"{first_difference(rom, base, expected)} If the change is intended, "
                        f"run python -m worlds.cvaos.test.golden_patches to record the new "
                        f"values, and say in the commit which seeds now patch differently.")

    def test_each_fixture_builds_its_rom_from_the_cart(self):
        self.check_base("cart")

    def test_each_fixture_builds_its_rom_from_the_collection(self):
        self.check_base("collection")


class FixtureLoadTest(unittest.TestCase):
    def test_the_folder_holds_each_fixture_and_its_expected_values(self):
        listed = {f"{name}{ending}" for name in FIXTURES for ending in (".apcvaos", ".json")}
        self.assertEqual(set(os.listdir(FOLDER)), listed)
        for name in FIXTURES:
            with self.subTest(fixture=name):
                self.assertEqual(set(load_expected(name)), set(BASES))

    def test_every_procedure_step_is_known(self):
        for name in FIXTURES:
            with self.subTest(fixture=name):
                container = _read(name)
                handler = AutoPatchExtensionRegister.get_handler(container.game)
                self.assertIs(handler, patch.CVAOSPatchExtension)
                for step, files in container.procedure:
                    self.assertTrue(callable(getattr(handler, step, None)), step)
                    self.assertLessEqual(set(files), set(container.files), step)

    def test_every_rom_config_key_is_known(self):
        for name in FIXTURES:
            with self.subTest(fixture=name):
                config = json.loads(_read(name).get_file("rom_config.json"))
                self.assertLessEqual(set(config), set(identifiers.ROM_CONFIG_KEY_IDENTIFIERS))


if __name__ == "__main__":
    unittest.main()
