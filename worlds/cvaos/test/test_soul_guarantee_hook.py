"""
Pins for the soul guarantee hook (``rom/soul_guarantee_hook.py``): the book-to-soul table, the
blob's layout, the veneer, the base-ROM check, and, when capstone is installed, a real
disassembly of the trampoline.

The tests also cover the pause-menu descriptions: which books gain the note that their soul always
drops, how the new strings are laid out, and whether and when the patch applies them.

    python -m pytest worlds/cvaos/test/test_soul_guarantee_hook.py -v
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from ..ram import addresses as addr
from ..rom import custom_pickups as cp
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

    def test_descriptions_sit_between_the_trampoline_and_the_icon_table(self):
        trampoline_end = sgh.GUARANTEE_TRAMPOLINE.addr + sgh.GUARANTEE_TRAMPOLINE.size
        self.assertGreaterEqual(sgh.BOOK_DESCRIPTIONS.addr, trampoline_end)
        self.assertLessEqual(sgh.BOOK_DESCRIPTIONS.addr + sgh.BOOK_DESCRIPTIONS.size,
                             cp.CUSTOM_ICON_TABLE_GBA)


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


# Each fixture book has a made-up two-line description at these text-ids and addresses, shaped like
# the vanilla ones. Each first line is padded to 33 characters, the length of a vanilla first line,
# so the strings are as long as the real ones. Book 1's string is then not a multiple of 4 bytes
# long, so alignment padding goes between it and Book 3's string.
FIXTURE_TEXT_IDS = {26: 374, 27: 375, 28: 376}
FIXTURE_STRINGS_GBA = 0x08500000
FIXTURE_FIRST_LINE = "{:<33}"


def menu_string(*lines: str) -> bytes:
    return b"\x01\x00" + b"\x06".join(line.encode("ascii") for line in lines) + b"\x06\x0a"


def first_line(slot: int) -> str:
    return FIXTURE_FIRST_LINE.format(f"Book in slot {slot}")


def rom_with_descriptions(last_line: str = "short") -> bytearray:
    """A blank ROM whose three books each have a description text-id and a two-line string."""
    rom = bytearray(ROM_SIZE)
    address = FIXTURE_STRINGS_GBA
    for slot, text_id in FIXTURE_TEXT_IDS.items():
        put(rom, sgh.DESC_TIDS_GBA + 2 * slot, text_id.to_bytes(2, "little"))
        put(rom, sgh.STRINGPTR_GBA + 4 * text_id, address.to_bytes(4, "little"))
        put(rom, address, menu_string(first_line(slot), last_line))
        address += 0x80
    return rom


def put(rom: bytearray, gba: int, data: bytes) -> None:
    offset = gba - GBA_ROM_BASE
    rom[offset:offset + len(data)] = data


def pointer_offset(text_id: int) -> int:
    """File offset of ``text_id``'s entry in the string-pointer array."""
    return sgh.STRINGPTR_GBA - GBA_ROM_BASE + 4 * text_id


def described_string(rom: bytes, slot: int) -> bytes:
    """Return the menu string that the string pointer for ``slot``'s fixture text-id points to."""
    at = pointer_offset(FIXTURE_TEXT_IDS[slot])
    start = int.from_bytes(rom[at:at + 4], "little") - GBA_ROM_BASE
    return bytes(rom[start:rom.index(b"\x06\x0a", start) + 2])


class DescriptionTest(unittest.TestCase):
    def test_only_books_whose_soul_an_enemy_drops_are_described(self):
        # No enemy row carries the Giant Bat soul (blue 2), because it is an item in the multiworld
        # pool.
        self.assertEqual(sgh.DESCRIBED_BOOKS, (BOOK_1, BOOK_3))
        self.assertNotIn((1, 2), {(t, i) for t, i, _rate in VANILLA.values()})

    def test_described_books_gain_the_note_and_book_2_keeps_its_string(self):
        rom = rom_with_descriptions()
        for offset, data in sgh.build_description_writes(bytes(rom)).items():
            rom[offset:offset + len(data)] = data
        for book in (BOOK_1, BOOK_3):
            self.assertEqual(described_string(rom, book.slot),
                             menu_string(first_line(book.slot), "short Its soul always drops."))
        self.assertEqual(described_string(rom, BOOK_2.slot), menu_string(first_line(27), "short"))

    def test_only_the_two_pointers_and_the_strings_are_written(self):
        writes = sgh.build_description_writes(bytes(rom_with_descriptions()))
        self.assertEqual(set(writes), {pointer_offset(374), pointer_offset(376),
                                       sgh.BOOK_DESCRIPTIONS.addr - GBA_ROM_BASE})

    def test_strings_are_word_aligned_inside_the_reservation(self):
        writes = sgh.build_description_writes(bytes(rom_with_descriptions()))
        strings = writes[sgh.BOOK_DESCRIPTIONS.addr - GBA_ROM_BASE]
        self.assertLessEqual(len(strings), sgh.BOOK_DESCRIPTIONS.size)
        first, second = (int.from_bytes(writes[pointer_offset(FIXTURE_TEXT_IDS[b.slot])], "little")
                         for b in sgh.DESCRIBED_BOOKS)
        book_1_length = len(menu_string(first_line(26), "short Its soul always drops."))
        self.assertNotEqual(book_1_length % 4, 0, "the fixture must need padding")
        self.assertEqual(first, sgh.BOOK_DESCRIPTIONS.addr)
        self.assertEqual(second, first + book_1_length + (-book_1_length % 4))

    def test_a_last_line_may_reach_exactly_the_box_width(self):
        # The real Book 1 line, "hell fires. Its soul always drops.", is exactly 34 characters.
        sgh.build_description_writes(bytes(rom_with_descriptions(last_line="x" * 11)))  # 11 + 23

    def test_a_line_too_long_for_the_box_is_refused(self):
        rom = rom_with_descriptions(last_line="x" * 12)   # 12 + 23 > 34
        with self.assertRaisesRegex(ValueError, "Ancient Book 1.*34 characters"):
            sgh.build_description_writes(bytes(rom))

    def test_a_pointer_to_no_string_is_a_rom_mismatch(self):
        rom = rom_with_descriptions()
        put(rom, sgh.STRINGPTR_GBA + 4 * FIXTURE_TEXT_IDS[26], (0x08600000).to_bytes(4, "little"))
        with self.assertRaisesRegex(ValueError, "ROM mismatch"):
            sgh.build_description_writes(bytes(rom))

    def test_a_terminated_string_without_the_menu_header_is_a_rom_mismatch(self):
        rom = rom_with_descriptions()
        put(rom, 0x08600000, b"\x00\x00ab\x06\x0a")
        put(rom, sgh.STRINGPTR_GBA + 4 * FIXTURE_TEXT_IDS[26], (0x08600000).to_bytes(4, "little"))
        with self.assertRaisesRegex(ValueError, "ROM mismatch"):
            sgh.build_description_writes(bytes(rom))


# rom_config.json with every optional feature off. run_apply_rom_features overrides entries of it.
FEATURES_OFF = {"skull_key_warp": False, "ancient_book_soul_drops": False,
                "forbidden_area_pickup": False, "soul_shuffle_plan": {},
                "multiply_soul_drop_rates": False, "single_jump_divekick": False,
                "classicvania_movement": False, "oops_all_whips": False}

# The builders, as (module, function), that need more of a real base ROM than a test fixture has.
STUBBED_BUILDERS = (("soul_guarantee_hook", "build_writes"), ("soul_shuffle", "build_writes"),
                    ("soul_drop_rates", "build_writes"), ("received_item_box", "build_writes"),
                    ("custom_pickups", "build_writes_from_rom"), ("inventory_menu", "build_writes"))


def run_apply_rom_features(test: unittest.TestCase, rom: bytes, **config) -> dict[str, mock.Mock]:
    """Run apply_rom_features on ``rom``, with ``config`` overriding entries of FEATURES_OFF.

    The config is passed as JSON, the way rom_config.json stores it. Each builder in
    STUBBED_BUILDERS is replaced by a stub that writes nothing, until ``test`` ends. Returns each
    stub by its module's name, so a test can check the arguments that builder received.
    """
    from ..rom import patch as rp
    caller = mock.Mock(get_file=lambda name: json.dumps({**FEATURES_OFF, **config}).encode())
    stubs = {}
    for module, function in STUBBED_BUILDERS:
        patcher = mock.patch.object(getattr(rp, module), function, return_value={})
        stubs[module] = patcher.start()
        test.addCleanup(patcher.stop)
    rp.CVAOSPatchExtension.apply_rom_features(caller, rom, "rom_config.json")
    return stubs


class PatchPipelineTest(unittest.TestCase):
    """apply_rom_features installs the book descriptions only when Ancient Book Soul Drops is on,
    and installs them before the Item-Use menu extension copies the string-pointer array."""

    def _rom_seen_by_the_menu_extension(self, books_on: bool) -> bytes:
        stubs = run_apply_rom_features(self, bytes(rom_with_descriptions()),
                                       ancient_book_soul_drops=books_on)
        return stubs["inventory_menu"].call_args.args[0]

    def test_the_menu_extension_copies_the_repointed_descriptions(self):
        rom = self._rom_seen_by_the_menu_extension(books_on=True)
        self.assertEqual(int.from_bytes(rom[pointer_offset(374):pointer_offset(374) + 4], "little"),
                         sgh.BOOK_DESCRIPTIONS.addr)

    def test_with_the_option_off_the_descriptions_stay_vanilla(self):
        rom = self._rom_seen_by_the_menu_extension(books_on=False)
        self.assertEqual(int.from_bytes(rom[pointer_offset(374):pointer_offset(374) + 4], "little"),
                         FIXTURE_STRINGS_GBA)
