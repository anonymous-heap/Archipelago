"""
Tests for the drop table (drop_sources.py) that the spoiler and the /drops command print.

Generation has no base ROM, so the spoiler runs the patch's own drop edits on a stand-in that holds
the vanilla drop fields. Its table must equal the one read back from the patched ROM, on each base
ROM. /drops reads the table from the ROM in the game's memory, on BizHawk and on the Advance
Collection, so it prints the spoiler's table too.

    python -m pytest worlds/cvaos/test/test_drop_sources.py -v
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from ..drop_sources import planned_drop_table_lines, rom_drop_table_lines
from .test_enemy_drop_rate import preset_seed
from .test_experimental_options import early
from .test_soul_sources import rom_reads, spoiler_sections


class SpoilerTest(unittest.TestCase):
    def test_the_spoiler_lists_what_the_patched_rom_holds(self):
        seed = preset_seed()
        if not seed.patched:
            self.skipTest("needs a USA Aria of Sorrow ROM")
        sections = spoiler_sections(seed.world)
        for name, (_before, after) in seed.patched.items():
            with self.subTest(base=name):
                self.assertEqual(sections["Item drops"], rom_drop_table_lines(after))

    def test_a_seed_that_changes_no_drop_writes_no_table(self):
        self.assertNotIn("Item drops", spoiler_sections(early(soul_shuffle="within_type")))

    def test_each_line_names_both_drops_and_their_chances(self):
        # Zombie's vanilla rarities are 20 and 30. The roll picks the common slot three times in
        # four, and then drops it with chance 4 / (20*4 + 16).
        lines = planned_drop_table_lines({})
        self.assertIn(
            "Zombie: Cloth Tunic (common, 1 in 32 kills), Baselard (rare, 1 in 136 kills)", lines)
        self.assertIn("Axe Armor: Leather Plate (common, 1 in 32 kills)", lines)


class DropsCommandTest(unittest.TestCase):
    """/drops on both clients prints the table that the patched ROM in the game holds."""

    def test_both_clients_print_the_spoilers_table(self):
        from .. import client, collection_client
        seed = preset_seed()
        if not seed.patched:
            self.skipTest("needs a USA Aria of Sorrow ROM")
        expected = "\n".join(["Item drops:", *(f"    {line}" for line in
                                                spoiler_sections(seed.world)["Item drops"])])
        for name, (_before, after) in seed.patched.items():
            read = rom_reads(after)
            bizhawk_read = mock.AsyncMock(side_effect=lambda _ctx, requests: read(requests))
            with self.subTest(base=name, client="BizHawk"), \
                    mock.patch("worlds._bizhawk.read", new=bizhawk_read), \
                    self.assertLogs("Client", level="INFO") as logs:
                asyncio.run(client.log_drop_table(SimpleNamespace(bizhawk_ctx=None)))
            self.assertEqual(logs.records[-1].getMessage(), expected)
            backend = SimpleNamespace(read_many=mock.AsyncMock(side_effect=read))
            with self.subTest(base=name, client="collection"), \
                    self.assertLogs("Client", level="INFO") as logs:
                asyncio.run(collection_client.log_drop_table(SimpleNamespace(backend=backend)))
            self.assertEqual(logs.records[-1].getMessage(), expected)

    def test_without_the_game_each_client_says_to_try_again(self):
        import worlds._bizhawk as bizhawk
        from .. import client, collection_client
        failing = mock.AsyncMock(side_effect=bizhawk.RequestFailedError("no connector"))
        with mock.patch("worlds._bizhawk.read", new=failing), \
                self.assertLogs("Client", level="INFO") as logs:
            asyncio.run(client.log_drop_table(SimpleNamespace(bizhawk_ctx=None)))
            asyncio.run(collection_client.log_drop_table(SimpleNamespace(backend=None)))
        self.assertEqual(len(logs.output), 2)
        self.assertTrue(all("try /drops again" in line for line in logs.output))

    def test_bizhawk_offers_drops_only_while_an_aria_of_sorrow_rom_is_loaded(self):
        from ..client import CVAOSClient, cmd_drops
        from ..collection_client import CVAOSCollectionContext
        ctx = mock.Mock()
        ctx.command_processor.commands = {}
        for valid in (True, False):
            checked = mock.AsyncMock(return_value=valid)
            with self.subTest(valid=valid), \
                    mock.patch.object(CVAOSClient, "_validate_rom", checked):
                asyncio.run(CVAOSClient().validate_rom(ctx))
                self.assertEqual(ctx.command_processor.commands.get("drops"),
                                 cmd_drops if valid else None)
        self.assertIn("drops", CVAOSCollectionContext.command_processor.commands)


if __name__ == "__main__":
    unittest.main()
