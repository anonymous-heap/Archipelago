"""
Tests for Hard Mode enforcement: the client forces the game-mode difficulty nibble to Hard so
the HARD_PICKUP entities actually spawn. See ram.accessors.ensure_hard_mode and client_logic._tick.

Run from the Archipelago root:
    python -m pytest worlds/cvaos/test/test_hard_mode.py -v
"""
from __future__ import annotations

import unittest

from ..ram import addresses as addr
from ..ram.accessors import AoSRAM, EWRAM
from .test_client_tick import FakeRam
from .test_deathlink import FakeCtx, _new_client
from .test_ram_map import FakeBackend

INGAME = int(addr.GameState.INGAME)

# EWRAM-domain offsets, as test_ram_map pins them.
GAME_MODE_OFFSET = 0x000A1
GAME_CLEARED_OFFSET = 0x00060


class WriteRecordingBackend(FakeBackend):
    """A FakeBackend that also records the offset of each plain write."""

    def __init__(self, memory: dict) -> None:
        super().__init__(memory)
        self.write_offsets: list[int] = []

    async def write(self, offset, data, domain):
        self.write_offsets.append(offset)
        await super().write(offset, data, domain)


async def run_ensure(ensure, offset: int, before: int) -> tuple[bool, int, list[int]]:
    """Run ``ensure``, an ``AoSRAM.ensure_*`` method, on EWRAM holding ``before`` at ``offset``.

    Returns what ``ensure`` returned, the byte at ``offset`` afterwards, and the offsets written.
    """
    ewram = bytearray(0x40000)
    ewram[offset] = before
    backend = WriteRecordingBackend({EWRAM: ewram})
    changed = await ensure(AoSRAM(backend))
    return changed, ewram[offset], backend.write_offsets


class EnsureRamStateTest(unittest.IsolatedAsyncioTestCase):
    async def test_hard_mode_forces_the_difficulty_nibble_and_keeps_the_character(self):
        # The high nibble is the difficulty (1 is Hard) and the low nibble the character.
        cases = {
            0x01: 0x11,   # Soma, normal
            0x11: 0x11,   # Soma, hard already
            0x00: 0x10,   # Julius, normal
            0x21: 0x11,   # any other difficulty
        }
        for before, after in cases.items():
            with self.subTest(before=hex(before)):
                changed, byte, written = await run_ensure(AoSRAM.ensure_hard_mode,
                                                          GAME_MODE_OFFSET, before)
                self.assertEqual(byte, after)
                self.assertEqual(changed, before != after)
                self.assertEqual(written, [GAME_MODE_OFFSET] if changed else [])

    async def test_game_cleared_sets_both_cleared_bits_and_keeps_the_rest(self):
        cases = {
            0x00: 0x03,   # not cleared
            0x03: 0x03,   # cleared already
            0x01: 0x03,   # one of the two bits
            0x04: 0x07,   # an unrelated bit
        }
        for before, after in cases.items():
            with self.subTest(before=hex(before)):
                changed, byte, written = await run_ensure(AoSRAM.ensure_game_cleared,
                                                          GAME_CLEARED_OFFSET, before)
                self.assertEqual(byte, after)
                self.assertEqual(changed, before != after)
                self.assertEqual(written, [GAME_CLEARED_OFFSET] if changed else [])


class ForcingRam(FakeRam):
    """test_client_tick's FakeRam, recording which RAM state _tick forces.

    Max HP is 0, which _tick reads as a save block that has not loaded yet, so it skips the
    save-block concerns. The forcing runs before that check.
    """

    def __init__(self, game_state: int, menu_state: int) -> None:
        super().__init__(game_state, menu_state, max_hp=0)
        self.forced: list[str] = []

    async def ensure_game_cleared(self) -> bool:
        self.forced.append("cleared")
        return False

    async def ensure_hard_mode(self) -> bool:
        self.forced.append("hard")
        return False


class TickForcingTest(unittest.IsolatedAsyncioTestCase):
    async def forced(self, *, hard_mode: int, game_state: int,
                     menu_state: int = addr.MENU_STATE_NORMAL) -> list[str]:
        ctx = FakeCtx(death_link=False)
        ctx.slot_data["hard_mode"] = hard_mode
        ram = ForcingRam(game_state, menu_state)
        await _new_client()._tick(ctx, ram)
        return ram.forced

    async def test_in_game_with_hard_mode_on_forces_both(self):
        self.assertEqual(await self.forced(hard_mode=1, game_state=INGAME), ["cleared", "hard"])

    async def test_in_game_with_hard_mode_off_forces_only_the_cleared_flag(self):
        # Every seed gets the cleared-data flag, so cutscenes can be skipped.
        self.assertEqual(await self.forced(hard_mode=0, game_state=INGAME), ["cleared"])

    async def test_nothing_forced_outside_ingame(self):
        title = int(addr.GameState.TITLE)
        self.assertEqual(await self.forced(hard_mode=1, game_state=title, menu_state=0), [])


if __name__ == "__main__":
    unittest.main()
