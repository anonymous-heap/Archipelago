"""
Pins for the soul guarantee hook (``rom/soul_guarantee_hook.py``): the book-to-soul table, the
blob's layout, the veneer, the base-ROM check, and, when capstone is installed, a real
disassembly of the trampoline.

    python -m pytest worlds/cvaos/test/test_soul_guarantee_hook.py -v
"""
from __future__ import annotations

import unittest

from ..ram import addresses as addr
from ..rom import soul_guarantee_hook as sgh
from ..rom.address_space import m2_region_hit
from ..rom.entity import GBA_ROM_BASE
from ..rom.soul_shuffle import VANILLA

try:
    import capstone
except ImportError:  # dev-only verifier; the byte pins below still run
    capstone = None

ROM_SIZE = 0x800000
SITE = sgh.HOOK_SITE.addr - GBA_ROM_BASE
TRAMP = sgh.GUARANTEE_TRAMPOLINE.addr - GBA_ROM_BASE
BOOK_1, BOOK_2, BOOK_3 = sgh.BOOK_SOULS


def rom_with_site() -> bytearray:
    rom = bytearray(ROM_SIZE)
    rom[SITE:SITE + len(sgh.STOLEN)] = sgh.STOLEN
    return rom


def thumb_branch_target(blob: bytes, at: int) -> int:
    """Target offset (within the blob) of the conditional branch encoded at ``at``."""
    word = int.from_bytes(blob[at:at + 2], "little")
    assert (word & 0xF000) == 0xD000, f"not a conditional branch at +{at:#x}: {word:#06x}"
    imm = word & 0xFF
    imm -= 0x100 if imm & 0x80 else 0
    return at + 4 + 2 * imm


class BookSoulsTest(unittest.TestCase):
    def test_books_are_the_consumable_slots_the_item_table_gives_them(self):
        self.assertEqual([b.slot for b in sgh.BOOK_SOULS], [26, 27, 28])
        first = addr.INVENTORY["consumable"].entry.item(BOOK_1.slot).request().offset
        self.assertEqual(sgh.BOOKS_EWRAM, addr.EWRAM_BASE + first)

    def test_souls_are_the_vanilla_table_entries_for_flame_demon_giant_bat_succubus(self):
        # Vanilla ids 104 (Flame Demon) and 95 (Succubus) carry the red 44 and yellow 7 souls.
        self.assertEqual(VANILLA[104][:2], (BOOK_1.soul_type, BOOK_1.soul_index))
        self.assertEqual(VANILLA[95][:2], (BOOK_3.soul_type, BOOK_3.soul_index))
        self.assertEqual((BOOK_2.soul_type, BOOK_2.soul_index), (1, 2))   # blue 2: Giant Bat


class PlacementTest(unittest.TestCase):
    def test_hook_site_and_trampoline_are_where_the_disassembly_says(self):
        self.assertEqual(sgh.HOOK_SITE.addr, 0x080684EE)
        self.assertEqual(sgh.GUARANTEE_TRAMPOLINE.addr, 0x08670600)
        self.assertEqual(sgh.DROP_RESUME, 0x080684F8)
        self.assertEqual(sgh.NODROP_RESUME, 0x0806852C)

    def test_trampoline_fits_its_reservation_and_is_clear_of_m2(self):
        self.assertLessEqual(len(sgh._TRAMPOLINE), 0x80)
        self.assertIsNone(m2_region_hit(TRAMP, 0x80))
        # the previous hook blob (inventory menu, 0x670500 + 212 bytes) ends before us
        self.assertGreaterEqual(TRAMP, 0x670500 + 212)


class BlobTest(unittest.TestCase):
    blob = sgh._TRAMPOLINE
    BOOK2, BOOK3, FORCE, ROLL, NODROP = 0x14, 0x22, 0x30, 0x32, 0x3C

    def test_branches_step_through_the_books_then_force_roll_or_nodrop(self):
        t = lambda at: thumb_branch_target(self.blob, at)  # noqa: E731
        self.assertEqual((t(0x08), t(0x0C), t(0x12)), (self.BOOK2, self.BOOK2, self.FORCE))
        self.assertEqual((t(0x16), t(0x1A), t(0x20)), (self.BOOK3, self.BOOK3, self.FORCE))
        self.assertEqual((t(0x24), t(0x28), t(0x2E)), (self.ROLL, self.ROLL, self.ROLL))
        self.assertEqual(t(0x34), self.NODROP)                              # the stolen bhs
        self.assertEqual(self.blob[self.FORCE:self.FORCE + 2], bytes.fromhex("0024"), "movs r4, #0")

    def test_literal_pool_words(self):
        self.assertEqual(sgh._POOL, (sgh.BOOKS_EWRAM, 0x080684F9, 0x0806852D))

    def test_pc_relative_loads_hit_the_pool(self):
        # ldr r0,[pc,#imm]: word-aligned PC + 4*imm must land on the pool words.
        for at, word_index in ((0x04, 0), (0x38, 1), (0x3C, 2)):
            insn = int.from_bytes(self.blob[at:at + 2], "little")
            self.assertEqual(insn & 0xFF00, 0x4800, f"ldr r0 literal expected at +{at:#x}")
            target = ((at + 4) & ~3) + 4 * (insn & 0xFF)
            self.assertEqual(target, 0x40 + 4 * word_index)

    @unittest.skipUnless(capstone, "capstone not installed")
    def test_disassembles_to_the_intended_program(self):
        md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
        listing = [f"{i.mnemonic} {i.op_str}".strip()
                   for i in md.disasm(self.blob[:0x40], sgh.GUARANTEE_TRAMPOLINE.addr)]
        self.assertEqual(listing, [
            "ldrb r2, [r6, #0x17]", "ldrb r3, [r6, #0x18]", "ldr r0, [pc, #0x38]",
            "cmp r2, #0", "bne #0x8670614", "cmp r3, #0x2c", "bne #0x8670614",
            "ldrb r1, [r0]", "cmp r1, #0", "bne #0x8670630",
            "cmp r2, #1", "bne #0x8670622", "cmp r3, #2", "bne #0x8670622",
            "ldrb r1, [r0, #1]", "cmp r1, #0", "bne #0x8670630",
            "cmp r2, #2", "bne #0x8670632", "cmp r3, #7", "bne #0x8670632",
            "ldrb r1, [r0, #2]", "cmp r1, #0", "beq #0x8670632",
            "movs r4, #0",
            "cmp r4, r5", "bhs #0x867063c", "subs r3, #1",
            "ldr r0, [pc, #8]", "bx r0",
            "ldr r0, [pc, #8]", "bx r0",
        ])


class VeneerAndWritesTest(unittest.TestCase):
    def test_veneer_jumps_to_the_trampoline_through_the_word_at_site_plus_6(self):
        veneer = sgh._far_jump_veneer(sgh.GUARANTEE_TRAMPOLINE)
        self.assertEqual(len(veneer), len(sgh.STOLEN))
        self.assertEqual(veneer[:6], bytes.fromhex("01480047c046"))   # ldr r0,[pc,#4] ; bx r0 ; nop
        self.assertEqual(int.from_bytes(veneer[6:], "little"), sgh.GUARANTEE_TRAMPOLINE.addr | 1)
        # ldr r0,[pc,#4] at a 2-mod-4 address reads (site+2)+4 = site+6
        self.assertEqual(((sgh.HOOK_SITE.addr + 4) & ~3) + 4, sgh.HOOK_SITE.addr + 6)

    def test_build_writes_installs_veneer_and_blob(self):
        writes = sgh.build_writes(bytes(rom_with_site()))
        self.assertEqual(set(writes), {SITE, TRAMP})
        self.assertEqual(writes[SITE], sgh._far_jump_veneer(sgh.GUARANTEE_TRAMPOLINE))
        self.assertEqual(writes[TRAMP], sgh._TRAMPOLINE)

    def test_build_writes_refuses_a_rom_without_the_stolen_instructions(self):
        rom = rom_with_site()
        rom[SITE] ^= 0xFF
        with self.assertRaisesRegex(ValueError, "ROM mismatch"):
            sgh.build_writes(bytes(rom))
