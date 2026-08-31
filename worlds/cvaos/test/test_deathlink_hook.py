"""
Offline invariants for the DeathLink "real kill" ROM hook patch bytes (no emulator needed).

These guard the pre-assembled trampoline/veneer in rom/deathlink_hook.py against accidental edits.
The instruction-level correctness was verified at authoring time with capstone (see
phase_4_6_planning/deathlink_real_kill.md); here we check the placement/wiring invariants that
must hold for the patch to be installed correctly.

    python -m pytest worlds/cvaos/test/test_deathlink_hook.py -v
"""
from __future__ import annotations

import unittest

from ..rom import deathlink_hook as dh
from ..rom.entity import GBA_ROM_BASE


class DeathlinkHookBytesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.writes = dh.build_writes()
        self.hook_off = dh.HOOK_SITE_GBA - GBA_ROM_BASE
        self.tramp_off = dh.TRAMPOLINE_GBA - GBA_ROM_BASE

    def test_offsets_match_design(self) -> None:
        self.assertEqual(self.hook_off, 0x1B9D0)    # _0801B9D0, the per-frame position/animation tail
        self.assertEqual(self.tramp_off, 0x660040)  # free ROM, just past the AP metadata block

    def test_two_writes_with_expected_lengths(self) -> None:
        self.assertEqual(set(self.writes), {self.hook_off, self.tramp_off})
        self.assertEqual(len(self.writes[self.hook_off]), 8)    # exactly the 8 stolen bytes
        self.assertEqual(len(self.writes[self.tramp_off]), 68)  # assembled trampoline

    def test_veneer_jumps_to_thumb_trampoline(self) -> None:
        veneer = self.writes[self.hook_off]
        # ldr r5,[pc,#0]; bx r5; .word TRAMPOLINE|thumb  (0x0801B9D0 is word-aligned)
        self.assertEqual(veneer[:4], bytes([0x00, 0x4D, 0x28, 0x47]))
        self.assertEqual(int.from_bytes(veneer[4:], "little"), dh.TRAMPOLINE_GBA | 1)
        self.assertEqual(dh.TRAMPOLINE_GBA & 1, 0, "trampoline base must be even; thumb bit added by veneer")

    def test_trampoline_references_kill_flag(self) -> None:
        self.assertIn(dh.KILL_REQUEST_EWRAM.to_bytes(4, "little"), self.writes[self.tramp_off])

    def test_writes_do_not_overlap(self) -> None:
        spans = sorted((off, off + len(data)) for off, data in self.writes.items())
        for (s0, e0), (s1, e1) in zip(spans, spans[1:]):
            self.assertLessEqual(e0, s1, "hook write regions must not overlap")


if __name__ == "__main__":
    unittest.main()
