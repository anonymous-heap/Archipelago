"""
Item-Use menu extension for Castlevania: Aria of Sorrow.

Makes the pause "Item Use" subscreen show the custom "key items" registered in ``custom_pickups.py``
(``CustomPickup`` with an ``inventory_name``) -- shown like CASTLE MAP 1 (name + 2-line description +
icon, non-usable) -- without touching the fixed 32-slot consumable inventory or save format.

How (all verified vs cvaos-decomp + the USA ROM; see inventory_menu.s for the trampoline write-up):

* Storage: the new items' "owned" bytes live in the unused, SRAM-saved pad_133A0 (gEwramData+0x133A0,
  76 bytes inside the 0x190 player struct). The collect hook (custom_pickups.s) writes pad_133A0[slot]
  = 1. No save-code change (it is already inside the persisted 0x190 block; old saves read it as 0).

* Menu read: the list builder sub_0804B494 (0x0804B494) and recount sub_0804B648 (0x0804B648) read
  the consumable counts as base+0x38 and iterate index 0..0x1f. Entry trampolines (inventory_menu.s)
  sync the real 32 counts into pad_133A0[0..31] and override the base to gEwramData+0x13368 so the
  loop reads a 47-slot shadow at 0x133A0; the index cap 0x1f is bumped to ``menu_slot_bound()``.

* Per-row data: the menu reads item-table (0x08505B3C), name (0x08506734) and description
  (0x08506936) tables; the use-gate sub_0804B36C treats item-table +8 >= 4 as a non-usable key item.
  We repoint the menu's copies of those literals to relocated/extended tables (custom_pickups builds
  the item table; here we relocate the name/desc text-id tables and the string-pointer array, append
  the new items' text-ids + strings). All string lookups (menu name, menu desc, and the "Got <name>"
  textbox) flow through the single resolver sub_08041434, whose one string-pointer-array literal
  (file 0x41470) we repoint -- so the appended strings resolve everywhere.

Emitted only when at least one inventory item is registered. patch.py calls ``build_writes``.
"""
from __future__ import annotations

import struct
from typing import Dict

from . import custom_pickups as cp
from .entity import GBA_ROM_BASE

# --- Source tables (vanilla) ---
NAME_TIDS_GBA = 0x08506734      # 257 u16, item global-id -> name text-id
DESC_TIDS_GBA = 0x08506936      # 257 u16, item global-id -> description text-id
STRINGPTR_GBA = 0x08506B38      # cp.STRINGPTR_COUNT u32, text-id -> string pointer
TEXT_ID_TABLE_ENTRIES = 257

# --- Relocated tables / blob placement (GBA addresses; clear of the custom_pickups region) ---
MENU_BLOB_GBA = 0x08660500          # inventory_menu.s trampolines
EXT_NAME_TABLE_GBA = 0x08661400     # relocated NAME_TIDS (+ new items' name text-ids)
EXT_DESC_TABLE_GBA = 0x08661700     # relocated DESC_TIDS (+ new items' desc text-ids)
EXT_STRINGPTR_GBA = 0x08662000      # relocated string-pointer array (+ appended new strings)
NEW_STRINGS_GBA = 0x08665000        # the new name/description string blobs

# --- Menu trampoline entry points (offsets within MENU_BLOB; from `nm` on inventory_menu.s) ---
MENU_LIST_HOOK_GBA = MENU_BLOB_GBA + 0x00      # MenuListHook   (over sub_0804B494)
MENU_RECOUNT_HOOK_GBA = MENU_BLOB_GBA + 0x24   # MenuRecountHook (over sub_0804B648)
B494_ENTRY_FILE = 0x0004B494
B648_ENTRY_FILE = 0x0004B648

# Assembled from inventory_menu.s at -Ttext=0x08660500 (THUMB). Position-DEPENDENT (literal pool bakes
# 0x02013294/0x020133A0/0x02013368 and the B494/B648 resume addresses). 92 bytes.
MENU_BLOB = bytes.fromhex(
    "02b4114a114b202011781970013201330138f9d102bc0e48f0b557464e4645460c4b1847"
    "02b4084a084b202011781970013201330138f9d102bc054870b58446002400230448004794"
    "320102a0330102683301029db4040851b60408"
)
assert len(MENU_BLOB) == 92, f"menu blob must be 92 bytes, got {len(MENU_BLOB)}"
assert cp.SHADOW_BASE_GBA.to_bytes(4, "little") in MENU_BLOB, "shadow base missing from menu blob"

# --- Menu instruction-literal sites to repoint (file offsets), confirmed against the ROM ---
# EVERY 0x08505B3C / 0x08506936 / 0x08506734 literal in the Item-Use cluster (0x4B36C..0x4B9D0) must
# be repointed -- including the USE-GATE sub_0804B36C (0x4B3C4) and sub_0804B808's *second* detail-
# render path (0x4B9B4/0x4B9B8). Missing the use-gate makes a key item read the original table's slot
# (= weapon[0]), so it heals 5 HP, is consumed, and shows the wrong name/desc/icon.
ITEMTBL_LITERALS = (0x0004B3C4, 0x0004B594, 0x0004B7AC, 0x0004B8E8, 0x0004B9B4)  # use-gate/list/detail*2/input
NAME_LITERAL = 0x0004B59C                                  # B494 name lookup
DESC_LITERALS = (0x0004B7B0, 0x0004B8EC, 0x0004B9B8)       # B6C4 / B808 (both detail paths) description
STRINGPTR_LITERAL = 0x00041470                             # the single resolver sub_08041434 base

# --- Index-cap sites (cmp r6/r2,#0x1f) -- one byte each (the 0x1f immediate) ---
B494_BOUND_SITES = (0x0004B4D0, 0x0004B4DA, 0x0004B58C, 0x0004B5DA, 0x0004B5F0)
B648_BOUND_SITES = (0x0004B674, 0x0004B67C, 0x0004B6A0, 0x0004B6A4, 0x0004B6B8)


def _veneer(target_gba: int) -> bytes:
    # ldr r3,[pc,#0]; bx r3; .word target|thumb (r3 is dead at the menu fn entries)
    return bytes.fromhex("004b1847") + struct.pack("<I", target_gba | 1)


def _encode_menu_string(text) -> bytes:
    """Menu string format: 01 00 + ASCII (0x06 = line break) + 06 0a terminator."""
    lines = (text,) if isinstance(text, str) else tuple(text)
    if len(lines) > 2 or any(len(line) > 34 for line in lines):
        raise ValueError("menu string is at most 2 lines of 34 chars")
    return b"\x01\x00" + b"\x06".join(line.encode("ascii") for line in lines) + b"\x06\x0a"


def build_writes(base_rom: bytes) -> Dict[int, bytes]:
    """``{rom_file_offset: bytes}`` installing the Item-Use menu extension. Empty if no inventory
    items are registered. ``base_rom`` is the full base ROM (to copy the relocated tables)."""
    items = cp.inventory_pickups()
    if not items:
        return {}

    # Safety net: every item/name/desc-table literal in the whole Item-Use cluster must be in our
    # repoint set, or a key item silently reads the original table (wrong data; usable -> heals/consumes).
    _expected = {0x08505B3C: set(ITEMTBL_LITERALS), 0x08506936: set(DESC_LITERALS),
                 0x08506734: {NAME_LITERAL}}
    for off in range(0x0004B36C, 0x0004B9D0, 4):
        v = int.from_bytes(base_rom[off:off + 4], "little")
        if v in _expected:
            assert off in _expected[v], (f"un-repointed menu table literal at file {off:#x} "
                                         f"(={v:#x}) -- add it to the repoint set")

    writes: Dict[int, bytes] = {}

    # 1. Trampolines + veneers over the menu builder / recount.
    writes[MENU_BLOB_GBA - GBA_ROM_BASE] = MENU_BLOB
    writes[B494_ENTRY_FILE] = _veneer(MENU_LIST_HOOK_GBA)
    writes[B648_ENTRY_FILE] = _veneer(MENU_RECOUNT_HOOK_GBA)

    # 2. Bump the index cap 0x1f -> bound at every cmp site (single-byte edits).
    bound = cp.menu_slot_bound()
    if bound > 0xFF:
        raise ValueError(f"menu slot bound {bound} exceeds the cmp #imm8 range")
    for off in B494_BOUND_SITES + B648_BOUND_SITES:
        assert base_rom[off] == 0x1F, f"expected 0x1f cap at {off:#x}, got {base_rom[off]:#x}"
        writes[off] = bytes([bound])

    # 3. Relocated NAME / DESC text-id tables (consumable-menu copies) with new items' text-ids.
    name_tbl = bytearray(base_rom[NAME_TIDS_GBA - GBA_ROM_BASE:
                                  NAME_TIDS_GBA - GBA_ROM_BASE + TEXT_ID_TABLE_ENTRIES * 2])
    desc_tbl = bytearray(base_rom[DESC_TIDS_GBA - GBA_ROM_BASE:
                                  DESC_TIDS_GBA - GBA_ROM_BASE + TEXT_ID_TABLE_ENTRIES * 2])

    # 4. Relocated string-pointer array (verbatim copy) + appended new name/desc pointers + strings.
    strptr = bytearray(base_rom[STRINGPTR_GBA - GBA_ROM_BASE:
                                STRINGPTR_GBA - GBA_ROM_BASE + cp.STRINGPTR_COUNT * 4])
    string_blob = bytearray()
    for i, p in enumerate(items):
        gid = p.item_offset                          # gid == slot for our items (indexes name/desc)
        n_tid, d_tid = cp.name_text_id(i), cp.desc_text_id(i)
        # point this gid's name/desc at the new text-ids
        name_tbl[gid * 2:gid * 2 + 2] = struct.pack("<H", n_tid)
        desc_tbl[gid * 2:gid * 2 + 2] = struct.pack("<H", d_tid)
        # append the two new string pointers (text-id i resolves to strptr[i])
        name_gba = NEW_STRINGS_GBA + len(string_blob)
        string_blob += _encode_menu_string(p.inventory_name)
        desc_gba = NEW_STRINGS_GBA + len(string_blob)
        string_blob += _encode_menu_string(p.description or (p.inventory_name,))
        assert n_tid == len(strptr) // 4 and d_tid == n_tid + 1, "text-id must equal the appended index"
        strptr += struct.pack("<I", name_gba) + struct.pack("<I", desc_gba)

    writes[EXT_NAME_TABLE_GBA - GBA_ROM_BASE] = bytes(name_tbl)
    writes[EXT_DESC_TABLE_GBA - GBA_ROM_BASE] = bytes(desc_tbl)
    writes[EXT_STRINGPTR_GBA - GBA_ROM_BASE] = bytes(strptr)
    writes[NEW_STRINGS_GBA - GBA_ROM_BASE] = bytes(string_blob)

    # 5. Repoint the menu's table literals to the relocated copies.
    for off in ITEMTBL_LITERALS:
        writes[off] = struct.pack("<I", cp.CUSTOM_ICON_TABLE_GBA)
    writes[NAME_LITERAL] = struct.pack("<I", EXT_NAME_TABLE_GBA)
    for off in DESC_LITERALS:
        writes[off] = struct.pack("<I", EXT_DESC_TABLE_GBA)
    writes[STRINGPTR_LITERAL] = struct.pack("<I", EXT_STRINGPTR_GBA)

    return writes
