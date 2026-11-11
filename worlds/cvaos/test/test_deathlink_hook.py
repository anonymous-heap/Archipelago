"""
Offline checks for the DeathLink "real kill" ROM hook bytes in rom/deathlink_hook.py.

These check the veneer's jump, the trampoline's handling of the Sky Fish bit, and that the
trampoline polls the same flag the client sets. test_rom_map pins the placement addresses.

    python -m pytest worlds/cvaos/test/test_deathlink_hook.py -v
"""
from __future__ import annotations

import unittest

from ..ram import addresses as addr
from ..rom import deathlink_hook as dh
from ..rom.entity import GBA_ROM_BASE


class DeathlinkHookBytesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.writes = dh.build_writes()
        self.hook_off = dh.HOOK_SITE_GBA - GBA_ROM_BASE
        self.tramp_off = dh.TRAMPOLINE_GBA - GBA_ROM_BASE

    def test_veneer_jumps_to_thumb_trampoline(self) -> None:
        veneer = self.writes[self.hook_off]
        # ldr r5,[pc,#0]; bx r5; .word TRAMPOLINE|thumb  (0x0801B9D0 is word-aligned)
        self.assertEqual(veneer[:4], bytes([0x00, 0x4D, 0x28, 0x47]))
        self.assertEqual(int.from_bytes(veneer[4:], "little"), dh.TRAMPOLINE_GBA | 1)
        self.assertEqual(dh.TRAMPOLINE_GBA & 1, 0, "trampoline base must be even; thumb bit added by veneer")

    def test_the_trampoline_polls_the_flag_the_client_sets(self) -> None:
        # The module asserts that the trampoline's literal pool holds KILL_REQUEST_EWRAM, and the
        # client writes addresses.KILL_REQUEST.
        self.assertEqual(dh.KILL_REQUEST_EWRAM, addr.KILL_REQUEST.addr)

    def test_trampoline_clears_the_sky_fish_bit_before_the_kill(self) -> None:
        # movs r0,#0x82; lsls r0,r0,#4; bics r2,r0 clears status bits 0x820, as the damage path
        # does before it calls the death handler. The handler skips the death animation while bit
        # 0x800 (set by the Sky Fish soul) is on.
        blob = self.writes[self.tramp_off]
        clear = bytes.fromhex("8220" "0001" "8243")
        handler_call = blob.index(bytes.fromhex("8e461847"))   # mov lr, r1; bx r3
        self.assertIn(clear, blob[:handler_call])


if __name__ == "__main__":
    unittest.main()
