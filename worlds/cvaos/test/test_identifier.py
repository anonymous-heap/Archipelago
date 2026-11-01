"""
The BizHawk client accepts a ROM patched by this version or by the previous compatible one, and
refuses any other.

Older clients cannot grant Progressive Movement items, so this version has its own identifier,
which they refuse. A ROM from the previous version holds nothing the current client cannot handle,
so the current client still accepts it.

    python -m pytest worlds/cvaos/test/test_identifier.py -v
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ..client import ROM_NAME, CVAOSClient
from ..rom import ARCHIPELAGO_IDENTIFIER


class IdentifierTest(unittest.IsolatedAsyncioTestCase):
    async def validate(self, identifier: str) -> bool:
        client = CVAOSClient.__new__(CVAOSClient)
        ctx = SimpleNamespace(bizhawk_ctx=None)
        reads = [ROM_NAME.encode("ascii"), identifier.encode("ascii")]
        with mock.patch("worlds._bizhawk.read", new=mock.AsyncMock(return_value=reads)):
            return await client.validate_rom(ctx)

    async def test_this_version_is_accepted(self) -> None:
        self.assertTrue(await self.validate(ARCHIPELAGO_IDENTIFIER))

    async def test_the_previous_version_is_accepted(self) -> None:
        self.assertTrue(await self.validate("CVAOS_AP_V0.3"))

    async def test_an_older_version_is_refused(self) -> None:
        self.assertFalse(await self.validate("CVAOS_AP_V0.2"))


if __name__ == "__main__":
    unittest.main()
