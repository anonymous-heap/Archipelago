"""
The collection client asks the server for the starting inventory as well as other worlds' items.

Logic counts the starting inventory as owned from the start, but nothing else puts it into the
game. A client that does not ask for it can therefore leave a seed unbeatable.

A seed with enemy checks also asks for the player's own items, because the ROM never hands out the
item at a defeat location. The installed ROM's identifier tells the client which kind of seed it
is. test_identifier.py checks the same choice in the BizHawk client.

    python -m pytest worlds/cvaos/test/test_items_handling.py -v
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ..rom import (ARCHIPELAGO_IDENTIFIER, ARCHIPELAGO_IDENTIFIER_START,
                   OWN_ITEMS_FROM_SERVER_IDENTIFIER)

OTHER_WORLDS = 0b001
OWN_ITEMS = 0b010
STARTING_INVENTORY = 0b100


class ItemsHandlingTest(unittest.IsolatedAsyncioTestCase):
    async def test_collection_client_reads_the_items_handling_from_the_installed_rom(self):
        from ..collection_client import CVAOSCollectionContext
        for identifier, expected in (
                (ARCHIPELAGO_IDENTIFIER, OTHER_WORLDS | STARTING_INVENTORY),
                (OWN_ITEMS_FROM_SERVER_IDENTIFIER, OTHER_WORLDS | OWN_ITEMS | STARTING_INVENTORY)):
            def read(address: int, size: int, identifier: str = identifier) -> bytes:
                if address == ARCHIPELAGO_IDENTIFIER_START:
                    return identifier.encode("ascii")[:size]
                return bytes(size)

            ctx = CVAOSCollectionContext.__new__(CVAOSCollectionContext)
            ctx.backend = SimpleNamespace(rom_base=0, proc=SimpleNamespace(read=read))
            ctx.auth, ctx.password = "auth", None
            ctx.send_connect = mock.AsyncMock()
            await ctx.server_auth()
            with self.subTest(identifier=identifier):
                self.assertEqual(ctx.items_handling, expected)
                ctx.send_connect.assert_awaited_once()

    def test_slot_data_tells_the_client_its_counter_includes_the_starting_inventory(self):
        # Without the key, the client treats the seed as an older one, whose saves count only
        # items from other worlds.
        from test.general import setup_multiworld
        from .. import CVAOSWorld
        multiworld = setup_multiworld(CVAOSWorld, seed=1)
        self.assertIs(multiworld.worlds[1].fill_slot_data()["starting_inventory_from_server"], True)


if __name__ == "__main__":
    unittest.main()
