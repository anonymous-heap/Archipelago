"""
The golden patch fixtures, and the script that records what each one must build.

Each fixture is a ``.apcvaos`` file in ``data/golden``, with a JSON file of the same name beside
it. For each base ROM, the JSON file holds the SHA-256 of the ROM that the patch builds from it.
The bases are the cart dump and the Advance Collection's copy. The file also holds a short digest
of every 16-byte block where the patched ROM differs from its base. A failing test uses those
digests to say where the first difference is, and they hold none of the game's bytes.

Run this module from the Archipelago folder to record the expected values again when a change to
the patch output is intended::

    python -m worlds.cvaos.test.golden_patches

Recording needs both base ROMs, and it leaves the fixtures as they are. ``--new-fixtures`` also
generates every fixture again from ``FIXTURES``. Only do that when a new release becomes the
baseline, because the fixtures stand for seeds that players already have.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys
import tempfile
from typing import NamedTuple

from ..rom import patch
from . import base_roms, generate

FOLDER = os.path.join(os.path.dirname(__file__), "data", "golden")
BASES = ("cart", "collection")
BLOCK_SIZE = 16
# The number of hex digits kept from each block's SHA-256. The full SHA-256 decides whether a test
# passes. The block digests only locate a difference, so a rare collision just reports a later
# offset.
BLOCK_DIGEST_DIGITS = 4


class Fixture(NamedTuple):
    """The seed and options that a fixture was generated with, and what it covers."""
    seed: int
    options: dict[str, object]
    covers: str


FIXTURES: dict[str, Fixture] = {
    "defaults": Fixture(1, {}, "the world's defaults"),
    "experimental_preset": Fixture(2, {
        "enemy_checks": "all", "soul_shuffle": "matched", "item_drop_shuffle": True,
        "enemy_drop_rate": 350, "wide_item_smoothing": True,
    }, "the Experimental preset"),
    "wide_vanilla_drops": Fixture(3, {
        "wide_item_smoothing": True, "item_drop_shuffle": False, "skull_key_warp": False,
        "ancient_book_soul_drops": False, "forbidden_area_button": "off",
        "single_jump_divekick": False,
    }, "Wide's band on the game's own drop table, with every patch feature that is on by default "
       "turned off"),
    "drop_shuffle_bosses": Fixture(4, {
        "item_drop_shuffle": True, "enemy_checks": "bosses",
    }, "Item Drop Shuffle with defeat checks on bosses only"),
    "soul_shuffle_any_type": Fixture(5, {
        "soul_shuffle": "any_type", "keep_soul_drop_rates": True, "shuffle_starting_soul": True,
    }, "Soul Shuffle across types, with each soul keeping its own rate and the starting soul "
       "shuffled"),
    "hard_mode": Fixture(6, {
        "hard_mode": True, "hard_mode_pickups": "always", "forbidden_area_button": "pickup",
        "ancient_book_soul_drops": True, "progressive_movement": True,
    }, "Hard Mode and its pickups, the Study Sealswitch pickup, Ancient Book soul drops and "
       "Progressive Movement"),
    "gameplay_tweaks": Fixture(7, {
        "single_jump_divekick": True, "classicvania_movement": True, "oops_all_whips": True,
        "multiply_soul_drop_rates": True, "soul_drop_rate_multiplier": 200, "death_link": True,
    }, "the gameplay tweaks, the soul-rate multiplier and the Death Link hook"),
    "advance_collection": Fixture(8, {
        "target_platform": "advance_collection", "soul_shuffle": "within_type",
        "keep_soul_drop_rates": False, "shuffle_starting_soul": False, "enemy_drop_rate": 200,
    }, "a collection seed, with Soul Shuffle within type that keeps each enemy's own rate and the "
       "vanilla starting soul, and Enemy Drop Rate on the game's own drop table"),
}


def patch_path(name: str) -> str:
    return os.path.join(FOLDER, f"{name}.apcvaos")


def expected_path(name: str) -> str:
    return os.path.join(FOLDER, f"{name}.json")


def load_expected(name: str) -> dict[str, dict]:
    """Return the fixture's expected values, by base name."""
    with open(expected_path(name), encoding="utf-8") as fh:
        return json.load(fh)


def apply_fixture(name: str, base: bytes) -> bytes:
    """Apply the fixture's patch to ``base`` the way a player's install does, and return the ROM.

    The collection's installer patches inside ``base_rom_override``. Opening a patch for BizHawk
    reads the base ROM that host.yaml names instead, and the override stands in for that file here.
    Every other step is the one a player's install runs.
    """
    with tempfile.TemporaryDirectory() as folder:
        out_path = os.path.join(folder, "patched.gba")
        with patch.base_rom_override(base):
            patch.CVAOSProcedurePatch(patch_path(name)).patch(out_path)
        with open(out_path, "rb") as fh:
            return fh.read()


def _block_digest(block: bytes) -> str:
    return hashlib.sha256(block).hexdigest()[:BLOCK_DIGEST_DIGITS]


def describe(rom: bytes, base: bytes) -> dict[str, object]:
    """Return the expected values for ``rom``, patched from ``base``.

    ``changed_blocks`` lists each run of blocks that differ from the base, as its file offset and
    its length in blocks. ``block_digests`` joins those blocks' digests in the same order.
    """
    runs: list[list] = []
    digests = []
    for start in range(0, len(rom), BLOCK_SIZE):
        block = rom[start:start + BLOCK_SIZE]
        if block == base[start:start + BLOCK_SIZE]:
            continue
        digests.append(_block_digest(block))
        if runs and int(runs[-1][0], 16) + runs[-1][1] * BLOCK_SIZE == start:
            runs[-1][1] += 1
        else:
            runs.append([f"{start:#08x}", 1])
    return {"sha256": hashlib.sha256(rom).hexdigest(), "changed_blocks": runs,
            "block_digests": "".join(digests)}


def first_difference(rom: bytes, base: bytes, expected: dict[str, object]) -> str:
    """Return where ``rom`` first differs from the ROM that ``expected`` describes.

    Outside the changed blocks the expected ROM equals ``base``, so the offset there is exact.
    Inside one, only the block is known.
    """
    if len(rom) != len(base):
        return f"The patched ROM is {len(rom):#x} bytes long, not {len(base):#x}."
    digits = BLOCK_DIGEST_DIGITS
    digests = iter(expected["block_digests"][i:i + digits]
                   for i in range(0, len(expected["block_digests"]), digits))
    changed = {}
    for offset, count in expected["changed_blocks"]:
        for index in range(count):
            changed[int(offset, 16) + index * BLOCK_SIZE] = next(digests)
    for start in range(0, len(rom), BLOCK_SIZE):
        block, original = rom[start:start + BLOCK_SIZE], base[start:start + BLOCK_SIZE]
        if start in changed:
            if _block_digest(block) != changed[start]:
                return (f"It first differs in the {BLOCK_SIZE} bytes at file offset {start:#08x}, "
                        f"which the patch writes.")
        elif block != original:
            offset = start + next(i for i, (a, b) in enumerate(zip(block, original)) if a != b)
            return (f"It first differs at file offset {offset:#08x}, which the patch should leave "
                    f"as it is in the base ROM.")
    return "The block digests match, so the difference sits in a block whose digest collided."


def write_fixtures() -> None:
    """Generate every fixture in FIXTURES again, and remove any other file in the folder."""
    os.makedirs(FOLDER, exist_ok=True)
    for stale in os.listdir(FOLDER):
        if os.path.splitext(stale)[0] not in FIXTURES:
            os.remove(os.path.join(FOLDER, stale))
    for name, fixture in FIXTURES.items():
        world = generate(fixture.seed, dict(fixture.options)).worlds[1]
        with tempfile.TemporaryDirectory() as folder:
            world.generate_output(folder)
            (generated,) = glob.glob(os.path.join(folder, "*.apcvaos"))
            os.replace(generated, patch_path(name))
        print(f"generated {name}")


def write_expected(roms: dict[str, bytes]) -> None:
    """Record each fixture's expected values on every base in ``roms``."""
    for name in FIXTURES:
        # One line per value keeps the long lists compact.
        entries = []
        for base in BASES:
            values = describe(apply_fixture(name, roms[base]), roms[base])
            fields = ",\n".join(f"    {json.dumps(key)}: {json.dumps(value)}"
                                for key, value in values.items())
            entries.append(f"  {json.dumps(base)}: {{\n{fields}\n  }}")
        with open(expected_path(name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("{\n" + ",\n".join(entries) + "\n}\n")
        print(f"recorded {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--new-fixtures", action="store_true",
                        help="generate the fixtures again before recording their values")
    args = parser.parse_args(argv)
    roms = base_roms()
    missing = [base for base in BASES if base not in roms]
    if missing:
        print(f"Recording needs every base ROM, and the test helpers found no {missing[0]} ROM.",
              file=sys.stderr)
        return 1
    if args.new_fixtures:
        write_fixtures()
    write_expected(roms)
    return 0


if __name__ == "__main__":
    sys.exit(main())
