"""
Tests for Castlevania: Aria of Sorrow, and the helpers they share.

``CVAOSTestBase`` runs Archipelago's standard world tests on this world with its default options.

These helpers find base ROMs. A test that reads a ROM skips when none is found.

* ``find_rom`` returns the path of a USA cart ROM.
* ``find_collection_rom`` returns the Advance Collection's copy of that ROM, as bytes.
* ``base_roms`` returns the contents of each ROM that was found, by name.

These helpers build seeds:

* ``early`` returns a world with the given options that has run only ``generate_early``.
* ``generate`` runs one world through the fill, the way Main does. ``BEFORE_FILL`` lists the
  steps it runs before ``pre_fill``.
* ``preset_seed`` generates one seed with the Experimental preset (``PRESET``), writes its patch
  and applies the patch to each base ROM found. It runs only once, and the tests share its result.

These helpers patch ROMs:

* ``run_apply_rom_features`` runs the patch's ``apply_rom_features`` step on a ROM. Every optional
  feature is off unless the test turns it on (``FEATURES_OFF``), and the builders in
  ``STUBBED_BUILDERS`` write nothing.
* ``apply_patch`` applies a patch file to a base ROM.

``SCRIPTED_BOSSES`` lists the enemy ids of the four bosses whose death scripts give their soul.

Pytest collects this module, so no helper's name may start with ``test``.
"""
import functools
import glob
import json
import os
import tempfile
import unittest
import zipfile
from typing import NamedTuple
from unittest import mock

from BaseClasses import LocationProgressType, MultiWorld
from Fill import (distribute_items_restrictive, distribute_planned_blocks, parse_planned_blocks,
                  resolve_early_locations_for_planned)
from test.bases import WorldTestBase
from test.general import setup_multiworld
from worlds.AutoWorld import call_all
from worlds.generic.Rules import exclusion_rules

from .. import CVAOSWorld
from ..options import cvaos_options_presets
from ..rom import patch


class CVAOSTestBase(WorldTestBase):
    game = "Castlevania - Aria of Sorrow"


USA_ROM_NAME = "Castlevania - Aria of Sorrow (USA)/Castlevania - Aria of Sorrow (USA).gba"


def find_rom() -> str | None:
    """Return the path of a USA Aria of Sorrow ROM for the tests that read one, or None.

    The tests look at the CVAOS_USA_ROM and CVAOS_ROM environment variables first, then at the
    ROM that host.yaml names, then at the Downloads folder. A test that needs the ROM skips
    without one.
    """
    candidates = [os.environ.get("CVAOS_USA_ROM", ""), os.environ.get("CVAOS_ROM", "")]
    try:
        from settings import get_settings
        candidates.append(get_settings().cvaos_options.rom_file)
    except Exception:
        pass
    candidates.append(os.path.join(os.path.expanduser("~"), "Downloads", USA_ROM_NAME))
    return next((path for path in candidates if path and os.path.isfile(path)), None)


def find_collection_rom() -> bytes | None:
    """Return the Advance Collection's stock AoS ROM for the tests that read one, or None.

    The ROM is extracted from the collection that host.yaml names, or from the one at the default
    Steam path. Extracting only reads the collection's files. A test that needs the ROM skips
    without one, and also when the collection holds no pristine copy of it.
    """
    import hashlib

    from ..advance_collection.install import extract_base_rom_bytes, is_collection_exe
    from ..constants import AC_DEFAULT_EXE_PATH, AC_USA_ROM_MD5

    candidates = []
    try:
        from settings import get_settings
        candidates.append(str(get_settings().cvaos_options.collection_exe))
    except Exception:
        pass
    candidates.append(AC_DEFAULT_EXE_PATH)
    for exe in candidates:
        if exe and os.path.isfile(exe) and is_collection_exe(exe):
            try:
                rom = extract_base_rom_bytes(exe)
            except Exception:
                return None
            return rom if hashlib.md5(rom).hexdigest() == AC_USA_ROM_MD5 else None
    return None


def base_roms() -> dict[str, bytes]:
    """Return each base ROM that a test can patch: "cart" for a cart dump, and "collection" for
    the Advance Collection's copy. A ROM that can't be found is left out."""
    roms = {}
    cart = find_rom()
    if cart is not None:
        with open(cart, "rb") as fh:
            roms["cart"] = fh.read()
    collection = find_collection_rom()
    if collection is not None:
        roms["collection"] = collection
    return roms


BEFORE_FILL = ("generate_early", "create_regions", "create_items", "set_rules",
               "connect_entrances", "generate_basic")


def generate(seed: int, options: dict) -> MultiWorld:
    """Generate one world through the fill the way Main does, including its location settings and
    item plando.

    Item smoothing is strict unless ``options`` sets it. Progression balancing and the output step
    do not run.
    """
    multiworld = setup_multiworld(CVAOSWorld, BEFORE_FILL[:4], seed=seed,
                                  options={"item_smoothing": "strict", **options})
    world = multiworld.worlds[1]
    excluded = world.options.exclude_locations.value
    exclusion_rules(multiworld, 1, excluded)
    existing = {location.name for location in multiworld.get_locations(1)}
    for name in (world.options.priority_locations.value - excluded) & existing:
        multiworld.get_location(name, 1).progress_type = LocationProgressType.PRIORITY
    multiworld.plando_item_blocks = parse_planned_blocks(multiworld)
    for step in BEFORE_FILL[4:]:
        call_all(multiworld, step)
    resolve_early_locations_for_planned(multiworld)
    distribute_planned_blocks(multiworld, multiworld.plando_item_blocks[1])
    call_all(multiworld, "pre_fill")
    distribute_items_restrictive(multiworld)
    call_all(multiworld, "post_fill")
    return multiworld


def early(**options) -> CVAOSWorld:
    """Return a one-player world with ``options`` that has run only generate_early."""
    return setup_multiworld(CVAOSWorld, ("generate_early",), seed=1, options=options).worlds[1]


# rom_config.json with every optional feature off. run_apply_rom_features overrides entries of it.
FEATURES_OFF = {"skull_key_warp": False, "ancient_book_soul_drops": False,
                "forbidden_area_pickup": False, "soul_shuffle_plan": {},
                "multiply_soul_drop_rates": False, "single_jump_divekick": False,
                "classicvania_movement": False, "oops_all_whips": False}

# The builders, as (module, function), that need more of a real base ROM than a test fixture has.
STUBBED_BUILDERS = (("soul_guarantee_hook", "build_writes"), ("soul_shuffle", "build_writes"),
                    ("soul_drop_rates", "build_writes"), ("received_item_box", "build_writes"),
                    ("custom_pickups", "build_writes_from_rom"), ("inventory_menu", "build_writes"))


def run_apply_rom_features(test: unittest.TestCase, rom: bytes, **config) -> dict[str, mock.Mock]:
    """Run apply_rom_features on ``rom``, with ``config`` overriding entries of FEATURES_OFF.

    The config is passed as JSON, the way rom_config.json stores it. Each builder in
    STUBBED_BUILDERS is replaced by a stub that writes nothing, until ``test`` ends. Returns each
    stub by its module's name, so a test can check the arguments that builder received.
    """
    caller = mock.Mock(get_file=lambda name: json.dumps({**FEATURES_OFF, **config}).encode())
    stubs = {}
    for module, function in STUBBED_BUILDERS:
        patcher = mock.patch.object(getattr(patch, module), function, return_value={})
        stubs[module] = patcher.start()
        test.addCleanup(patcher.stop)
    patch.CVAOSPatchExtension.apply_rom_features(caller, rom, "rom_config.json")
    return stubs


PRESET = cvaos_options_presets["Experimental"]
# Headhunter, Death, Legion and Balore, whose death scripts give their soul.
SCRIPTED_BOSSES = (106, 107, 108, 109)


def apply_patch(patch_path: str, base: bytes, folder: str) -> tuple[bytes, bytes]:
    """Apply the patch at ``patch_path`` to ``base``, and return the ROM just before the
    enemy-table step and the finished ROM."""
    out_path = os.path.join(folder, "patched.gba")
    step = patch.CVAOSPatchExtension.apply_enemy_table
    with patch.base_rom_override(base), mock.patch.object(
            patch.CVAOSPatchExtension, "apply_enemy_table", wraps=step) as spy:
        patch.CVAOSProcedurePatch(patch_path).patch(out_path)
    with open(out_path, "rb") as fh:
        return spy.call_args.args[1], fh.read()


class PresetSeed(NamedTuple):
    world: CVAOSWorld
    config: dict  # the patch's rom_config.json
    patched: dict[str, tuple[bytes, bytes]]  # base name -> ROM before the last step, and after


@functools.cache
def preset_seed() -> PresetSeed:
    """Generate one seed with the Experimental preset, write its patch, and apply the patch to each
    available base ROM. The result is shared by every test that reads it."""
    world = generate(2, dict(PRESET)).worlds[1]
    with tempfile.TemporaryDirectory() as folder:
        world.generate_output(folder)
        (patch_path,) = glob.glob(os.path.join(folder, "*.apcvaos"))
        with zipfile.ZipFile(patch_path) as patch_file:
            config = json.loads(patch_file.read("rom_config.json"))
        patched = {name: apply_patch(patch_path, base, folder)
                   for name, base in base_roms().items()}
    return PresetSeed(world, config, patched)
