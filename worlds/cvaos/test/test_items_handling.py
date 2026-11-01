"""
Both clients ask the server for the starting inventory as well as other worlds' items.

Logic counts the starting inventory as owned from the start, but nothing else puts it into the
game. A client that does not ask for it can therefore leave a seed unbeatable.

    python -m pytest worlds/cvaos/test/test_items_handling.py -v
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ..client import ROM_NAME, CVAOSClient
from ..rom import ARCHIPELAGO_IDENTIFIER

OTHER_WORLDS = 0b001
STARTING_INVENTORY = 0b100


class ItemsHandlingTest(unittest.IsolatedAsyncioTestCase):
    async def test_bizhawk_client_asks_for_other_worlds_items_and_the_starting_inventory(self):
        client = CVAOSClient.__new__(CVAOSClient)
        ctx = SimpleNamespace(bizhawk_ctx=None)
        reads = [ROM_NAME.encode("ascii"), ARCHIPELAGO_IDENTIFIER.encode("ascii")]
        with mock.patch("worlds._bizhawk.read", new=mock.AsyncMock(return_value=reads)):
            self.assertTrue(await client.validate_rom(ctx))
        self.assertEqual(ctx.items_handling, OTHER_WORLDS | STARTING_INVENTORY)

    def test_collection_client_asks_for_other_worlds_items_and_the_starting_inventory(self):
        from ..collection_client import CVAOSCollectionContext
        self.assertEqual(CVAOSCollectionContext.items_handling, OTHER_WORLDS | STARTING_INVENTORY)

    def test_slot_data_tells_the_client_its_counter_includes_the_starting_inventory(self):
        # Without the key, the client treats the seed as an older one, whose saves count only
        # items from other worlds.
        from test.general import setup_multiworld
        from .. import CVAOSWorld
        multiworld = setup_multiworld(CVAOSWorld, seed=1)
        self.assertIs(multiworld.worlds[1].fill_slot_data()["starting_inventory_from_server"], True)


if __name__ == "__main__":
    unittest.main()
