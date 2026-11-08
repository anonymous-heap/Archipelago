"""
Guaranteed soul drops: the Ancient Book soul-drop hook for Castlevania: Aria of Sorrow.

Each Ancient Book describes one of the true-ending souls, in the game's own words: Book 1 "a demon
from hell fires" (Flame Demon), Book 2 "the King of Bats" (Giant Bat), Book 3 "a beautiful
nightmare" (Succubus). With this hook installed, whichever enemy drops one of those souls always
drops it while the matching book is in the inventory. The check runs entirely in the ROM, so no
client is involved and the behavior is the same in any emulator.

The enemy-death routine decides a soul drop at 0x080684C8..0x080684F0: it rolls
``(Random() >> 2) mod max(16, rate*8 + 32 - LCK/16)`` and drops when the roll is below the
numerator (3 if the soul is owned, 6 if not, 7 on Hard, +8 with the Soul Eater Ring). Two facts
about that code shape this hook, both read off the disassembly and confirmed on a live game:

* A rate byte of 0 does not guarantee a drop. It skips the roll entirely, and the table uses it
  for bosses, whose own scripts grant their souls.
* The denominator never goes below 16 and the numerator never reaches it, so no table value can
  make a drop certain. Only the comparison can.

So the 10 bytes at 0x080684EE (``cmp r4,r5 ; bhs nodrop ; ldrb r2,[r6,#0x17] ; ldrb r3,[r6,#0x18] ;
subs r3,#1``) become a far jump to a trampoline in free ROM. On entry r4 is the roll, r5 the
numerator and r6 the dying enemy's table row. The trampoline reads the row's soul type and index.
If they name a book's soul and that book's count byte in the consumable inventory is nonzero, it
forces the roll to 0, which is below every numerator. Then the stolen instructions run and control
returns to the drop or no-drop path exactly as before. Matching on the soul rather than the enemy
means Soul Shuffle needs nothing extra: whichever enemy carries the soul now is the one affected.

In this randomizer the Giant Bat soul is an item in the multiworld pool, and no row of the enemy
table carries it. The trampoline still tests for it, but that test never matches, so Book 2 has
no effect. ``build_description_writes`` adds one sentence to the pause-menu descriptions of Book 1
and Book 3, saying that their soul always drops. Book 2's description is unchanged because the
hook does nothing for it.

The trampoline bytes are assembled from ``soul_guarantee_hook.s`` at DEV time by
``rom/thumb_assembler.py``, so AP generation needs no toolchain. test_thumb_assembler.py asserts
the bytes below are what that source assembles to, so the two cannot drift apart.

Placement is declared as two ``Entry`` objects: the hook site bounded to its 10 stolen bytes and
the trampoline to its free-ROM reservation. ``build_writes`` checks the stolen bytes against the
base ROM before writing anything, so a ROM with a different code layout is refused.
"""
from __future__ import annotations

from typing import Dict, NamedTuple

from .._bytemaker_compat import Entry, Patch, UInt8, count, u16, u32, unknown
from .address_space import gba_space
from .custom_pickups import STRINGPTR_COUNT
from .entity import GBA_ROM_BASE
from .inventory_menu import DESC_TIDS_GBA, STRINGPTR_GBA, TEXT_ID_TABLE_ENTRIES
from .soul_shuffle import VANILLA


class BookSoul(NamedTuple):
    slot: int         # consumable inventory index of the book
    book: str
    soul: str
    soul_type: int    # enemy table +0x17: 0 red, 1 blue, 2 yellow, 3 ability
    soul_index: int   # enemy table +0x18: 1-based within the type


# In blob order: the trampoline tests these three in sequence and reads BOOKS_EWRAM + i for the
# i-th book's count, so the slots must be consecutive and ascending.
BOOK_SOULS: tuple[BookSoul, ...] = (
    BookSoul(26, "Ancient Book 1", "Flame Demon", 0, 44),
    BookSoul(27, "Ancient Book 2", "Giant Bat", 1, 2),
    BookSoul(28, "Ancient Book 3", "Succubus", 2, 7),
)
# EWRAM byte of Ancient Book 1's count: the consumable inventory (ram.addresses INVENTORY
# "consumable", GBA 0x02013294) at item 26. Pinned against the address map by the tests.
BOOKS_EWRAM = 0x020132AE

# --- placement ---
HOOK_SITE = Entry(0x080684EE, UInt8, count(10), name="soul guarantee hook site",
                  note="enemy-death soul roll: cmp r4,r5 ; bhs ; ldrb r2,[r6,#0x17] ; "
                       "ldrb r3,[r6,#0x18] ; subs r3,#1 (10 bytes)")
GUARANTEE_TRAMPOLINE = Entry(0x08670600, UInt8, unknown("soul_guarantee_hook.s blob"), reserve=0x80,
                             name="SOUL_GUARANTEE_TRAMPOLINE",
                             note="free ROM after the inventory-menu blob (0x670500+212); the "
                                  "extended icon table starts at 0x671000")

# What the base ROM holds at HOOK_SITE, and where the two paths continue after the stolen bytes.
STOLEN = bytes.fromhex("ac421cd2f27d337e013b")
DROP_RESUME = HOOK_SITE.addr + len(STOLEN)   # movs r0,#0 ... bl sub_0804459C (spawn the soul)
NODROP_RESUME = 0x0806852C                    # the item-drop logic the stolen bhs targeted

# Trampoline assembled from soul_guarantee_hook.s at 0x08670600 (THUMB, ARMv4T). 76 bytes:
# 32 instructions + a 3-word literal pool (BOOKS_EWRAM, DROP_RESUME|1, NODROP_RESUME|1).
# The pool holds only addresses outside the blob and every branch stays inside it, so the same
# bytes work at any word-aligned address. Only the veneer's jump target ties the blob to
# GUARANTEE_TRAMPOLINE.addr.
_TRAMPOLINE = bytes.fromhex(
    "f27d337e0e48002a04d12c2b02d1017800290dd1012a04d1022b02d14178002906d1022a05d1072b03d1"
    "8178002900d00024ac4202d2013b0248004702480047ae320102f98406082d850608"
)
_POOL = tuple(int.from_bytes(_TRAMPOLINE[i:i + 4], "little")
              for i in range(len(_TRAMPOLINE) - 12, len(_TRAMPOLINE), 4))

# Layout invariants, stated against the declarations: an edit to either side fails here before it
# reaches a ROM.
assert len(_TRAMPOLINE) == 76, f"trampoline must be 76 bytes, got {len(_TRAMPOLINE)}"
assert BOOKS_EWRAM in _POOL, "book-count addr missing from literal pool"
assert (DROP_RESUME | 1) in _POOL, "blob does not resume on the drop path after the stolen bytes"
assert (NODROP_RESUME | 1) in _POOL, "blob does not resume on the no-drop path"
assert (_TRAMPOLINE[0:4] == STOLEN[4:8] and _TRAMPOLINE[0x32:0x34] == STOLEN[0:2]
        and _TRAMPOLINE[0x36:0x38] == STOLEN[8:10]), \
    "blob must re-execute the stolen instructions (the bhs is re-encoded for its new distance)"
for _i, _book in enumerate(BOOK_SOULS):
    # cmp r2,#type ; cmp r3,#index ; ldrb r1,[r0,#i]  (Thumb-1 encodings)
    assert bytes([_book.soul_type, 0x2A]) in _TRAMPOLINE, f"{_book.book}: soul type test missing"
    assert bytes([_book.soul_index, 0x2B]) in _TRAMPOLINE, f"{_book.book}: soul index test missing"
    assert (0x7801 | (_i << 6)).to_bytes(2, "little") in _TRAMPOLINE, f"{_book.book}: count read missing"
    assert _book.slot == BOOK_SOULS[0].slot + _i, "book slots must be consecutive"


def _far_jump_veneer(target: Entry) -> bytes:
    """The 10 bytes that overwrite the hook site: ``ldr r0,[pc,#4] ; bx r0 ; nop ; .word target|thumb``.

    The site is 2 mod 4, so the load's word-aligned PC is the site + 2 and ``#4`` lands on the
    word at site + 6, right after the ``nop``. r0 is dead there (the roll was already copied to
    r4). The word comes from ``target``'s declaration, so the jump cannot drift from the blob."""
    return bytes([0x01, 0x48, 0x00, 0x47, 0xC0, 0x46]) + (target.addr | 1).to_bytes(4, "little")


def build_writes(base_rom: bytes) -> Dict[int, bytes]:
    """``{rom_file_offset: bytes}`` installing the hook: the veneer over the soul roll's
    comparison and the trampoline in its free-ROM reservation. Call only when the seed enables
    Ancient Book soul drops.

    The hook site must still hold the five instructions (10 bytes) the veneer replaces. A base ROM
    that does not, because its code layout differs, raises ``ValueError`` rather than being
    patched blind.
    """
    # Recorded blind against the cart geometry, as the other hooks are, so each blob stays one
    # write. A space over the ROM bytes would leave out the bytes that already match, splitting
    # the blob into several writes.
    space = gba_space()
    offset, size = HOOK_SITE.bind(space).request()
    if base_rom[offset:offset + size] != STOLEN:
        raise ValueError(
            f"soul guarantee hook site at file {offset:#x} holds {base_rom[offset:offset + size].hex()}, "
            f"expected {STOLEN.hex()} (ROM mismatch)")
    patch = Patch(name="soul guarantee hook")
    HOOK_SITE.bind(space).write(_far_jump_veneer(GUARANTEE_TRAMPOLINE), patch=patch)
    GUARANTEE_TRAMPOLINE.bind(space).write(_TRAMPOLINE, patch=patch)
    return {edit.offset: edit.new for edit in patch.edits}


# --- pause-menu descriptions ---
# All item text resolves through one string-pointer array, indexed by text-id. A book's
# description is extended by writing a new string to free ROM and repointing only that book's
# entry, the same way the Skull Key Warp option replaces the Skull Key's description. The vanilla
# text is read from the player's ROM, so the apworld carries only the added sentence.
DESCRIPTION_NOTE = b" Its soul always drops."
ITEM_DESCRIPTION_TEXT_IDS = Entry(DESC_TIDS_GBA, u16, count(TEXT_ID_TABLE_ENTRIES),
                                  name="item description text-ids",
                                  note="indexed by item number; a consumable's number is its slot")
STRING_POINTERS = Entry(STRINGPTR_GBA, u32, count(STRINGPTR_COUNT), name="string pointers",
                        note="indexed by text-id")
BOOK_DESCRIPTIONS = Entry(0x08670680, UInt8, unknown("extended book descriptions"), reserve=0x100,
                          name="BOOK_DESCRIPTIONS",
                          note="free ROM after the trampoline's reservation (0x670600+0x80)")

# A menu string is 01 00, then ASCII lines separated by 06, then the 06 0a terminator. The
# description box holds two lines of up to 34 characters, the same limit skull_key_warp and
# inventory_menu enforce. _with_note raises rather than write a longer line.
_MENU_STRING_START = b"\x01\x00"
_MENU_STRING_END = b"\x06\x0a"
_LINE_BREAK = b"\x06"
_MENU_LINE_MAX = 34


def _soul_dropped_by_an_enemy(book: BookSoul) -> bool:
    # A rate of 0 skips the drop roll, and the hook only acts on that roll.
    return any((soul_type, soul_index) == (book.soul_type, book.soul_index) and rate != 0
               for soul_type, soul_index, rate in VANILLA.values())


# The books whose description gains DESCRIPTION_NOTE: those whose soul an enemy drops through the
# drop roll. Soul Shuffle never moves these progression souls, so checking the vanilla table gives
# the right result for every seed.
DESCRIBED_BOOKS: tuple[BookSoul, ...] = tuple(b for b in BOOK_SOULS if _soul_dropped_by_an_enemy(b))


def _menu_string_at(base_rom: bytes, pointer: int) -> bytes:
    """The whole menu string at GBA address ``pointer``, terminator included."""
    start = pointer - GBA_ROM_BASE
    end = base_rom.find(_MENU_STRING_END, start, start + 0x100)
    if end < 0 or not base_rom.startswith(_MENU_STRING_START, start):
        raise ValueError(f"no menu string at {pointer:#x} (ROM mismatch)")
    return base_rom[start:end + len(_MENU_STRING_END)]


def _with_note(description: bytes, book: BookSoul) -> bytes:
    """``description`` with :data:`DESCRIPTION_NOTE` appended to its last line."""
    lines = description[len(_MENU_STRING_START):-len(_MENU_STRING_END)].split(_LINE_BREAK)
    lines[-1] += DESCRIPTION_NOTE
    if len(lines[-1]) > _MENU_LINE_MAX:
        raise ValueError(f"{book.book}: {lines[-1]!r} is longer than the description box's "
                         f"{_MENU_LINE_MAX} characters")
    return _MENU_STRING_START + _LINE_BREAK.join(lines) + _MENU_STRING_END


def build_description_writes(base_rom: bytes) -> Dict[int, bytes]:
    """``{rom_file_offset: bytes}`` that add a sentence to the pause-menu description of each book
    in :data:`DESCRIBED_BOOKS`. Call only when the seed enables Ancient Book soul drops.

    Each book keeps its vanilla description and gains :data:`DESCRIPTION_NOTE` on its last line.
    The new strings go to :data:`BOOK_DESCRIPTIONS`, and each book's string pointer is repointed
    at its copy. Apply these before the inventory-menu extension, which relocates the pointer
    array by copying it from the ROM as it stands.

    Raises ``ValueError`` when a book's pointer does not lead to a menu string (ROM mismatch), or
    when the note would make a line longer than the description box allows.
    """
    space = gba_space(base_rom)
    text_ids = ITEM_DESCRIPTION_TEXT_IDS.bind(space)
    pointers = STRING_POINTERS.bind(space)
    geometry = gba_space()
    patch = Patch(name="Ancient Book descriptions")
    strings = bytearray()
    for book in DESCRIBED_BOOKS:
        text_id = text_ids.item(book.slot).read()
        description = _with_note(_menu_string_at(base_rom, pointers.item(text_id).read()), book)
        STRING_POINTERS.bind(geometry).item(text_id).write(BOOK_DESCRIPTIONS.addr + len(strings),
                                                           patch=patch)
        strings += description + bytes(-len(description) % 4)  # keep each string word-aligned
    BOOK_DESCRIPTIONS.bind(geometry).write(bytes(strings), patch=patch)
    return {edit.offset: edit.new for edit in patch.edits}
