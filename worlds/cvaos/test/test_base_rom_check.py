"""
Applying a patch checks the base ROM's MD5 before anything else reads it. It also warns when a
seed made for the collection is patched from a cart dump.

    python -m pytest worlds/cvaos/test/test_base_rom_check.py -v
"""
from __future__ import annotations

import hashlib
import unittest
from unittest import mock

from ..options import TARGET_ADVANCE_COLLECTION, TARGET_GBA
from ..rom import patch as rp


class BaseRomCheckTest(unittest.TestCase):
    def test_an_accepted_base_is_returned_unchanged(self):
        base = b"an accepted base"
        with mock.patch.object(rp.CVAOSProcedurePatch, "hash", [hashlib.md5(base).hexdigest()]):
            self.assertEqual(rp.CVAOSProcedurePatch._checked_base(base), base)

    def test_any_other_base_is_refused_with_its_md5(self):
        base = b"a previous seed's patched rom"
        with self.assertRaisesRegex(ValueError, hashlib.md5(base).hexdigest()):
            rp.CVAOSProcedurePatch._checked_base(base)

    def test_both_ways_of_sourcing_the_base_are_checked(self):
        with mock.patch.object(rp, "get_base_rom_bytes", return_value=b"wrong"):
            with self.assertRaisesRegex(ValueError, "not an unmodified"):
                rp.CVAOSProcedurePatch.get_source_data()
            patch = rp.CVAOSProcedurePatch.__new__(rp.CVAOSProcedurePatch)
            with mock.patch.object(rp.CVAOSProcedurePatch, "get_file",
                                   return_value=b'{"target_platform": 0}', create=True):
                with self.assertRaisesRegex(ValueError, "not an unmodified"):
                    patch.get_source_data_with_cache()


class TargetBaseTest(unittest.TestCase):
    """A cart dump is an accepted base for either target, with a warning when the seed was made
    for the collection."""

    def setUp(self) -> None:
        # The check reads the cart dump's hash from USA_ROM_MD5, so b"cart" stands in for one.
        patcher = mock.patch.object(rp, "USA_ROM_MD5", hashlib.md5(b"cart").hexdigest())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_collection_seed_patched_from_a_cart_dump_warns(self) -> None:
        # The result plays in BizHawk. The collection's installer refuses it anyway, so the check
        # only warns.
        with self.assertLogs("Patch", level="WARNING") as logs:
            self.assertEqual(rp.check_base_for_target(b"cart", TARGET_ADVANCE_COLLECTION), b"cart")
        self.assertIn("Collection ROM Installer", logs.output[0])

    def test_a_gba_seed_accepts_a_cart_dump_without_a_warning(self) -> None:
        with self.assertNoLogs("Patch", level="WARNING"):
            self.assertEqual(rp.check_base_for_target(b"cart", TARGET_GBA), b"cart")


if __name__ == "__main__":
    unittest.main()
