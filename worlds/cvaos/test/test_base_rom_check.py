"""
Applying a patch checks the base ROM's MD5 before anything else reads it.

    python -m pytest worlds/cvaos/test/test_base_rom_check.py -v
"""
from __future__ import annotations

import hashlib
import unittest
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
