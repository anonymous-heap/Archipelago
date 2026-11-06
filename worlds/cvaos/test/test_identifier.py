"""
The BizHawk client accepts a ROM patched by this version or by the previous compatible one, and
refuses any other. The ROM's identifier also sets which items the client asks the server for.

Older clients cannot grant Progressive Movement items, so this version has its own identifier,
which they refuse. A ROM from the previous version holds nothing the current client cannot handle,
so the current client still accepts it.

The client asks for the starting inventory as well as other worlds' items, because logic counts the
starting inventory as owned from the start and nothing else puts it into the game. A seed with enemy
checks also asks for the player's own items, because the ROM never hands out the item at a defeat
location.

    python -m pytest worlds/cvaos/test/test_identifier.py -v
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ..client import ROM_NAME, CVAOSClient
from ..rom import ARCHIPELAGO_IDENTIFIER, OWN_ITEMS_FROM_SERVER_IDENTIFIER

OTHER_WORLDS = 0b001
OWN_ITEMS = 0b010
STARTING_INVENTORY = 0b100


class IdentifierTest(unittest.IsolatedAsyncioTestCase):
    async def validate(self, identifier: str) -> tuple[bool, SimpleNamespace]:
        """Run validate_rom on a ROM that carries ``identifier``, and return its result and the
        context it filled in."""
        client = CVAOSClient.__new__(CVAOSClient)
        ctx = SimpleNamespace(bizhawk_ctx=None)
        reads = [ROM_NAME.encode("ascii"), identifier.encode("ascii")]
        with mock.patch("worlds._bizhawk.read", new=mock.AsyncMock(return_value=reads)):
            return await client.validate_rom(ctx), ctx

    async def test_this_version_asks_for_other_worlds_items_and_the_starting_inventory(self):
        valid, ctx = await self.validate(ARCHIPELAGO_IDENTIFIER)
        self.assertTrue(valid)
        self.assertEqual(ctx.items_handling, OTHER_WORLDS | STARTING_INVENTORY)

    async def test_a_seed_with_enemy_checks_also_asks_for_own_items(self):
        valid, ctx = await self.validate(OWN_ITEMS_FROM_SERVER_IDENTIFIER)
        self.assertTrue(valid)
        self.assertEqual(ctx.items_handling, OTHER_WORLDS | OWN_ITEMS | STARTING_INVENTORY)

    async def test_the_previous_version_is_accepted_like_this_version(self) -> None:
        valid, ctx = await self.validate("CVAOS_AP_V0.3")
        self.assertTrue(valid)
        self.assertEqual(ctx.items_handling, OTHER_WORLDS | STARTING_INVENTORY)

    async def test_an_older_version_is_refused(self) -> None:
        valid, _ctx = await self.validate("CVAOS_AP_V0.2")
        self.assertFalse(valid)


if __name__ == "__main__":
    unittest.main()
