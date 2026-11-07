"""
Tests for the soul table (soul_sources.py) that the spoiler and the /souls command print.

Generation has no base ROM, so the spoiler runs the patch's own soul edits on a stand-in that holds
the vanilla soul bytes. Its table must equal the one read back from a patched ROM, for every soul
shuffle mode and rate option, on each base ROM. /souls reads the table from the ROM in the game's
memory, on BizHawk and on the Advance Collection, so it prints the spoiler's table too.

    python -m pytest worlds/cvaos/test/test_soul_sources.py -v
"""
from __future__ import annotations

import asyncio
import io
import re
import unittest
from random import Random
from types import SimpleNamespace
from unittest import mock

from ..rom import patch, soul_shuffle
from ..rom.enemy_table import TABLE_OFFSET, TABLE_SIZE, parse_table
from ..soul_sources import (planned_soul_table_lines, rom_soul_table_lines, soul_name,
                            vanilla_soul_name)
from . import base_roms
from .test_enemy_drop_rate import SCRIPTED_BOSSES, preset_seed
from .test_experimental_options import early


def config_for(mode: str, seed: int, **options) -> dict:
    """A rom_config.json for the soul edits, with a plan for ``mode`` drawn from ``seed``."""
    groups = patch.matched_groups() if mode == soul_shuffle.MODE_MATCHED else None
    plan = soul_shuffle.plan_shuffle(Random(seed), mode, groups)
    return {"soul_shuffle_plan": {str(target): source for target, source in plan.items()},
            "keep_soul_drop_rates": False, "shuffle_starting_soul": True,
            "multiply_soul_drop_rates": False, "soul_drop_rate_multiplier": 100, **options}


def spoiler_sections(world) -> dict[str, list[str]]:
    """Return each table that ``world`` writes into the spoiler, by title, as unindented lines."""
    spoiler = io.StringIO()
    world.write_spoiler(spoiler)
    sections = {}
    for block in re.split(r"\n\n(?=\S)", spoiler.getvalue())[1:]:
        head, *lines = block.rstrip("\n").split("\n")
        title = head.removesuffix(f" ({world.player_name}):")
        sections[title] = [line.removeprefix("    ") for line in lines]
    return sections


class SpoilerTest(unittest.TestCase):
    def test_the_spoiler_lists_what_the_patched_rom_holds(self):
        seed = preset_seed()
        if not seed.patched:
            self.skipTest("needs a USA Aria of Sorrow ROM")
        sections = spoiler_sections(seed.world)
        boss_souls = {vanilla_soul_name(boss) for boss in SCRIPTED_BOSSES}
        for name, (_before, after) in seed.patched.items():
            lines = rom_soul_table_lines(after)
            with self.subTest(base=name):
                self.assertEqual(sections["Soul sources"], lines)
                # Live test 9 compares the host of a boss soul, so this seed must have one.
                rows = parse_table(after[TABLE_OFFSET:TABLE_OFFSET + TABLE_SIZE])
                hosts = [enemy_id for enemy_id, row in enumerate(rows)
                         if row.soul_index and enemy_id not in SCRIPTED_BOSSES
                         and soul_name(row.soul_type, row.soul_index) in boss_souls]
                self.assertTrue(hosts)
                self.assertTrue(all(rows[host].soul_rate for host in hosts))

    def test_a_seed_that_changes_no_soul_writes_no_table(self):
        spoiler = io.StringIO()
        early().write_spoiler(spoiler)
        self.assertEqual(spoiler.getvalue(), "")


class PlannedTableTest(unittest.TestCase):
    def test_the_planned_table_matches_the_patched_rom_for_every_option(self):
        bases = base_roms()
        if not bases:
            self.skipTest("needs a USA Aria of Sorrow ROM")
        drop_rates = {str(enemy_id): [350, 142] for enemy_id in range(0, 113, 3)}
        configs = {
            "within_type, rates move": config_for(soul_shuffle.MODE_WITHIN_TYPE, 1,
                                                  keep_soul_drop_rates=True,
                                                  multiply_soul_drop_rates=True,
                                                  soul_drop_rate_multiplier=400),
            "any_type": config_for(soul_shuffle.MODE_ANY_TYPE, 2),
            "matched with drop rates": config_for(soul_shuffle.MODE_MATCHED, 3,
                                                  enemy_drop_rates=drop_rates),
        }
        for base_name, base in bases.items():
            for name, config in configs.items():
                real = bytearray(base)
                patch.apply_soul_edits(real, config)
                patch.apply_drop_rate_edits(real, config)
                with self.subTest(base=base_name, config=name):
                    self.assertEqual(planned_soul_table_lines(config),
                                     rom_soul_table_lines(bytes(real)))


def rom_reads(rom: bytes):
    """Return a stand-in for a backend's reads, which serves ROM-domain reads from ``rom``."""
    def read(requests) -> list[bytes]:
        assert all(domain == "ROM" for _offset, _size, domain in requests)
        return [rom[offset:offset + size] for offset, size, _domain in requests]
    return read


class SoulsCommandTest(unittest.TestCase):
    """/souls on both clients prints the table that the patched ROM in the game holds."""

    def test_both_clients_print_the_spoilers_table(self):
        from .. import client, collection_client
        seed = preset_seed()
        if not seed.patched:
            self.skipTest("needs a USA Aria of Sorrow ROM")
        expected = "\n".join(["Soul sources:", *(f"    {line}" for line in
                                                  spoiler_sections(seed.world)["Soul sources"])])
        for name, (_before, after) in seed.patched.items():
            read = rom_reads(after)
            bizhawk_read = mock.AsyncMock(side_effect=lambda _ctx, requests: read(requests))
            with self.subTest(base=name, client="BizHawk"), \
                    mock.patch("worlds._bizhawk.read", new=bizhawk_read), \
                    self.assertLogs("Client", level="INFO") as logs:
                asyncio.run(client.log_soul_table(SimpleNamespace(bizhawk_ctx=None)))
            self.assertEqual(logs.records[-1].getMessage(), expected)
            backend = SimpleNamespace(read_many=mock.AsyncMock(side_effect=read))
            with self.subTest(base=name, client="collection"), \
                    self.assertLogs("Client", level="INFO") as logs:
                asyncio.run(collection_client.log_soul_table(SimpleNamespace(backend=backend)))
            self.assertEqual(logs.records[-1].getMessage(), expected)

    def test_without_the_game_each_client_says_to_try_again(self):
        import worlds._bizhawk as bizhawk
        from .. import client, collection_client
        failing = mock.AsyncMock(side_effect=bizhawk.RequestFailedError("no connector"))
        with mock.patch("worlds._bizhawk.read", new=failing), \
                self.assertLogs("Client", level="INFO") as logs:
            asyncio.run(client.log_soul_table(SimpleNamespace(bizhawk_ctx=None)))
            asyncio.run(collection_client.log_soul_table(SimpleNamespace(backend=None)))
        self.assertEqual(len(logs.output), 2)
        self.assertTrue(all("try /souls again" in line for line in logs.output))

    def test_bizhawk_offers_souls_only_while_an_aria_of_sorrow_rom_is_loaded(self):
        from ..client import CVAOSClient, cmd_souls
        from ..collection_client import CVAOSCollectionContext
        ctx = mock.Mock()
        ctx.command_processor.commands = {}
        for valid in (True, False):
            checked = mock.AsyncMock(return_value=valid)
            with self.subTest(valid=valid), \
                    mock.patch.object(CVAOSClient, "_validate_rom", checked):
                asyncio.run(CVAOSClient().validate_rom(ctx))
                self.assertEqual(ctx.command_processor.commands.get("souls"),
                                 cmd_souls if valid else None)
        self.assertIn("souls", CVAOSCollectionContext.command_processor.commands)


if __name__ == "__main__":
    unittest.main()
