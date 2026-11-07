"""
Custom-pickup framework for Castlevania: Aria of Sorrow.

A custom pickup is a floor pickup whose collection runs its own behaviour instead of granting an
item: it sets a memory flag and plays a sound, and one with an ``inventory_name`` also shows a
"Got <name>" textbox. No existing item changes. Each custom item is one ``CustomPickup`` row in
custom_pickup_content.py, so adding another takes only data: a row, a 16x16 icon and a placement.
The first is the Study Sealswitch. Collecting it sets MISC flag #48, the same flag the A01
press-button (object 0x35) writes, so the paired barrier opens. It also plays the button's sound
(song 0x133). See ``custom_pickups.s`` for the full write-up.

How it fits together (all verified against the USA ROM):

* New item space. Custom items are consumables (subtype 2) at ``item_offset >= 32``. The
  consumable-icon lookup in the spawn path (sub_08044054) loads its table base from one literal at
  file 0x440B4 (0x08505B3C). That literal is repointed to an extended table in free ROM, which holds
  the 32 original entries copied verbatim followed by the custom entries at offsets 32 and up.
  inventory_menu.py repoints the Item-Use menu to the same table, while the collect-grant and shop
  paths keep the original. Consumables 0..31 read identical entries from either table, so nothing
  the player can already get changes.

* Custom icons in unused slots. Icon ids 0x1f..0x40 are unused, since consumables end at 0x1e and
  weapons start at 0x41. That leaves 34 free 16x16 slots in icon sheet 0, which is stored raw and
  DMA'd. Each custom entry's +2 byte names one of those slots, and the build writes the tiles
  straight into it, so no sheet moves. Each pickup declares an icon source (see rom/icon.py). A
  ``RomSprite`` is extracted from the player's own ROM at build time, for art already in the game
  such as the metal-gate button, so only an address, a region and a palette-index remap ship. An
  ``ImageFile`` is original art vendored with the apworld. build_writes_from_rom resolves both to
  bank-6 tiles, so the apworld itself contains no ROM-derived graphics.

* Table-driven behaviour. The collect hook (custom_pickups.s, installed over sub_080441F8) checks
  that the pickup is a consumable, then scans DESC_TABLE for the pickup's var_b. On a match it
  queues the "Got <name>" textbox for an inventory item, sets the row's flag, plays its sound and
  despawns the pickup. It then jumps to the vanilla finish, which writes the 0x360 "collected" flag
  from var_a and returns without granting anything. Any other pickup resumes the vanilla collect
  path unchanged.

* Palette. Custom icons use OBJ palette bank 6, the shared palette for item pickups. It is loaded
  for every floor pickup in every room, so it is never overwritten. Bank 6 is sub-palette 2 of the
  items palette: the loader sub_0800C5D8 calls
  ``sub_0803C7B4(0x082099FC, sUnk_084F0DD8[bank], 1, bank)``, which copies from
  ``0x082099FC + 4 + sub*0x20`` (a 4-byte header precedes the data), and sUnk_084F0DD8[6] == 2.
  DSVEdit agrees: an item's packed "Icon" field is ``(palette_bank<<8)|icon``, and it renders with
  sub-palette ``bank-4``, which is 2. Sub-palette 2 has a wide range of 16 colours (gold #F8D848,
  tan #F8B070, orange and red, blues and greens, greys #A0A0A8 and #E8D8D0, near-black), so most
  icons can be drawn in it directly by quantising their tiles against BANK6_PALETTE below. No
  per-item palette is written, and each item entry's +3 byte stays 6. A spare OBJ bank (10-15)
  cannot hold a per-item palette, because enemy and effect sprites reuse those banks and the
  colours break in busy rooms.

rom/patch.py applies these writes in every seed, and this module only builds the bytes. A location
spawns a custom pickup only when patch.py encodes it with ``get_encoding``.

The dispatcher blob is assembled from custom_pickups.s, beside this file. No literal in its pool
is an address inside the blob, and every branch is local, so the bytes are the same at any
word-aligned link address. The pool does hold DESC_TABLE's address, so moving the descriptor table
means changing DESC_TABLE in the .s to match CUSTOM_DESC_TABLE_GBA below and reassembling. From
rom/, with keystone-engine installed:

    python thumb_assembler.py custom_pickups.s 0x08670300   # -> CUSTOMHOOK_BLOB
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .entity import GBA_ROM_BASE
from .icon import RomSprite, ImageFile, build_icon_tiles

# --- Hook site: the pickup on-collision/collect callback sub_080441F8 ---
# We overwrite its first 8 bytes (push {r4-r7,lr}; mov r7,r8; push {r7}; sub sp,#4), which the hook
# replicates. 0x080441F8 is word-aligned so the ldr/bx/.word veneer's [pc,#0] reads the target word.
HOOK_SITE_GBA = 0x080441F8

# --- Free-ROM placement (GBA addresses; clear of identifier/auth/deathlink/skull-key at 0x670000..0x67026F,
# and of the Advance Collection ROM's M2 additions at 0x660000-0x6610BC / 0x700000-0x7000E3) ---
CUSTOMHOOK_BASE_GBA = 0x08670300        # the dispatcher blob
CUSTOM_DESC_TABLE_GBA = 0x08670400      # descriptor table (baked into the blob's literal pool)
CUSTOM_ICON_TABLE_GBA = 0x08671000      # extended consumable-icon table (repoint target)

# --- Vanilla consumable-icon table (the spawn-path lookup we extend) ---
CONSUMABLE_TABLE_GBA = 0x08505B3C
CONSUMABLE_ENTRY_SIZE = 0x10
CONSUMABLE_COUNT = 32                    # entries 0..31; table is 0x200 bytes
CONSUMABLE_ICON_LITERAL_FILE_OFFSET = 0x440B4   # `ldr r0,=0x08505B3C` in sub_08044054 (subtype-2 branch)

# Weapon + armor item tables (only used to verify a custom icon id doesn't collide with a real item's
# icon). Counts are table-to-table (contiguous: consumable .. weapon .. armor .. name-table 0x08506734).
# Verified in the USA ROM: consumable icon bytes are 0x01..0x1e, weapon 0x41..0x7a and armor
# 0x81..0xb4, so 0x1f..0x40 is genuinely free (the spawn icon byte is each entry's +2, as for
# consumables).
WEAPON_TABLE_GBA = 0x08505D3C
WEAPON_ENTRY_SIZE = 0x1C
WEAPON_COUNT = 59                        # (0x085063B0 - 0x08505D3C) / 0x1C
ARMOR_TABLE_GBA = 0x085063B0
ARMOR_ENTRY_SIZE = 0x14
ARMOR_COUNT = 45                         # (0x08506734 - 0x085063B0) / 0x14
ITEM_ENTRY_ICON_OFF = 0x2               # +2 = icon id, +3 = OBJ palette bank
ITEM_ENTRY_PAL_OFF = 0x3
ITEMS_PALETTE_BANK = 6                   # shared item-pickup OBJ palette (loaded for every floor pickup)
ITEMS_PALETTE_GBA = 0x082099FC           # items-palette block (16 sub-palettes; +4 header, then sub*0x20)

# OBJ bank 6's actual 16 colours (items-palette sub-palette 2 of 0x082099FC; see the module docstring).
# Quantise custom icon tiles against these. Index 0 is transparent in a 4bpp icon (the colour here is
# only what bank-6 slot 0 happens to hold). Dump from the USA ROM:
#   sub_0803C7B4 reads 0x082099FC + 4 + 2*0x20 = 0x08209A40, 16 BGR555 hwords.
BANK6_PALETTE = (0x2145, 0x0463, 0x138c, 0x456c, 0x5694, 0x6b7d, 0x01e6, 0x277f,   # incl. #F8D848 gold (idx7)
                 0x6523, 0x7e29, 0x7f51, 0x14c9, 0x0c96, 0x0d9f, 0x3adf, 0x7fff)   # #F8B070 tan(14), #A0A0A8 grey(4)

# --- Common-icon sheets (sub_0801232C); a 16x16 icon = top 0x40 @ off, bottom 0x40 @ off+0x200 ---
ICON_SHEETS_GBA = (0x081C5E00, 0x081C7E04, 0x081C9E08)   # 64 icons/sheet
ICON_HALF = 0x40

# Dispatcher blob, assembled from custom_pickups.s at 0x08670300 (THUMB, ARMv4T). Its literal pool
# holds 0x08670400 (CUSTOM_DESC_TABLE_GBA), 0x02000000 (gEwramData), 0x0800ef99 (sub_0800EF98|1,
# the "Got <name>" textbox), 0x080d7911 (PlaySong|1), the 0xFFFF terminator, and the vanilla
# resume and finish addresses 0x08044203/0x08044509. None of these is inside the blob, so it can
# sit at any word-aligned address, but CUSTOM_DESC_TABLE_GBA cannot change without reassembling.
CUSTOMHOOK_BLOB = bytes.fromhex(
    "f0b5474680b481b0041c3620205c022809d1658e164e30881649884203d0a84204d00c36f7e7201c134b18473089002802d0124b00f01bf87188b2881048401853099b00c0181f21114001238b40016819430160f088002802d00a4b00f007f85920215c082211432154074b1847184700046708ffff00000342040899ef00080000000211790d0809450408"
)
assert len(CUSTOMHOOK_BLOB) == 140, f"hook blob must be 140 bytes, got {len(CUSTOMHOOK_BLOB)}"
assert CUSTOM_DESC_TABLE_GBA.to_bytes(4, "little") in CUSTOMHOOK_BLOB, "desc-table addr missing from blob"

# Flag-field byte offsets from gEwramData (0x02000000). The hook computes
#   dword = gEwram + field + (flag_number>>5)*4 ;  *dword |= 1 << (flag_number & 0x1F)
FLAG_FIELD_EVENT = 0x33C     # SetEventFlag field
FLAG_FIELD_MISC = 0x344      # the A01 button's field
FLAG_FIELD_PICKUP = 0x360    # per-location "collected" field
FLAG_FIELD_BOSS = 0x37E      # boss-death field

DESC_TERMINATOR = b"\xff\xff\x00\x00\x00\x00\x00\x00"


# The Item-Use shadow inventory is a TRANSIENT array in the free high-EWRAM tail above the gEwramData
# struct. The struct ends at 0x02025554 and EWRAM ends at 0x02040000, and game code never touches
# that tail after the boot zero-fill. The array is NOT SRAM-saved. Instead, the menu trampolines
# (inventory_menu.py) rebuild it on every build and recount: slots 0..31 mirror the real
# consumables, and each custom slot is derived from that item's saved behaviour flag (its DESC_TABLE
# row's flag_field/flag_number). The saved flag is therefore the only record of ownership.
# pad_133A0 (0x133A0..0x133EB) MUST NOT hold the shadow, because all of it is soul-system storage:
# the bestiary bitfield at 0x133A0..0x133BF and the equip and sorted-soul lists at 0x133D0..0x133E7.
# A shadow there would corrupt the saved soul data, and soul updates would overwrite the shadow.
SHADOW_INVENTORY_GBA = 0x02030000       # transient; gEwramData+0x30000, far above the struct (0x25554)
SHADOW_BASE_GBA = 0x0202FFC8            # base s.t. base+0x38 == SHADOW_INVENTORY_GBA
MAX_SHADOW_SLOT = 0x2B                  # 44-slot shadow (0x02030000..0x0203002B); ample free space here
USE_TYPE_KEYITEM = 0x04                 # item-table +8: >=4 => shown but not usable (like Castle Maps)
ITEM_ENTRY_USE_OFF = 0x8
ITEM_ENTRY_GID_OFF = 0x0


@dataclass(frozen=True)
class CustomPickup:
    """A floor pickup that sets a flag and plays a sound instead of granting an item.

    ``item_offset`` is the pickup's var_b, unique and in 32..0x2B. ``icon_id`` is a free icon id in
    0x1f..0x40, also unique. ``icon`` is the icon source, a ``RomSprite`` or an ``ImageFile`` (see
    rom/icon.py).

    On collection the hook sets bit ``flag_number`` of the ``flag_field`` array and plays ``sfx``
    (0 = silent). It then despawns the floating pickup and marks it collected through its var_a
    0x360 flag.

    When ``inventory_name`` is set, the pickup is also a key item like CASTLE MAP 1. It occupies
    Item-Use slot ``item_offset``, where it is shown but cannot be used. ``inventory_name`` (at most
    18 characters) names it in the "Got <name>" textbox and the menu row, and ``description`` (at
    most 2 lines of 34 characters) fills the detail panel. inventory_menu.py writes both strings
    into the ROM. When ``inventory_name`` is None, the pickup has no menu row and no textbox."""
    name: str
    item_offset: int
    icon_id: int
    icon: object                    # icon source: RomSprite (extracted from the player's ROM) | ImageFile
    flag_field: int
    flag_number: int
    sfx: int
    inventory_name: Optional[str] = None
    description: Optional[Tuple[str, ...]] = None

    def __post_init__(self) -> None:
        if not (CONSUMABLE_COUNT <= self.item_offset <= MAX_SHADOW_SLOT):
            raise ValueError(f"{self.name}: item_offset {self.item_offset} must be in "
                             f"[{CONSUMABLE_COUNT}, {MAX_SHADOW_SLOT}]")
        if not (0x1F <= self.icon_id <= 0x40):
            raise ValueError(f"{self.name}: icon_id {self.icon_id:#x} must be in free range 0x1f..0x40")
        if not isinstance(self.icon, (RomSprite, ImageFile)):
            raise ValueError(f"{self.name}: icon must be a RomSprite or ImageFile, got {type(self.icon).__name__}")
        if self.inventory_name is not None and len(self.inventory_name) > 18:
            # Vanilla item names run to about 14 characters. Up to 18 is accepted, though a name
            # that long may crowd the count column.
            raise ValueError(f"{self.name}: inventory_name must be <= 18 chars")
        if self.description is not None and (len(self.description) > 2
                                             or any(len(line) > 34 for line in self.description)):
            raise ValueError(f"{self.name}: description must be <= 2 lines of <= 34 chars")


# --- Registry access (CONTENT lives in custom_pickup_content.py) ------------------------------------
# To ADD A PICKUP, edit custom_pickup_content.py rather than this file. This module imports that
# registry LAZILY, at call time, because custom_pickup_content imports this module at load and an
# import at the top of this file would be circular.
def _registry() -> List[CustomPickup]:
    from . import custom_pickup_content
    return custom_pickup_content.CUSTOM_PICKUPS


def validate_registry(pickups: List[CustomPickup]) -> None:
    """Raise ValueError if two pickups share an ``item_offset`` or an ``icon_id``.

    ``item_offset`` is both a pickup's shadow slot and its global id, which indexes the name and
    description tables, so a duplicate would hide one pickup's menu row. ``icon_id`` is a slot in
    the shared icon sheet, so a duplicate would overwrite one pickup's icon. custom_pickup_content
    calls this after building its CUSTOM_PICKUPS list."""
    offsets = [p.item_offset for p in pickups]
    icons = [p.icon_id for p in pickups]
    if len(set(offsets)) != len(offsets):
        raise ValueError(f"duplicate custom-pickup item_offset(s): {sorted(offsets)}")
    if len(set(icons)) != len(icons):
        raise ValueError(f"duplicate custom-pickup icon_id(s): {[hex(i) for i in sorted(icons)]}")


# Re-export the content registry's names, so ``custom_pickups.CUSTOM_PICKUPS``,
# ``.STUDY_SEALSWITCH`` and ``.CUSTOM_PICKUP_TEST_PLACEMENTS`` resolve here as well, as tests and
# patch.py expect. The module-level __getattr__ (PEP 562) imports them on first access, which
# avoids the same import cycle as _registry.
_CONTENT_NAMES = ("CUSTOM_PICKUPS", "STUDY_SEALSWITCH", "CUSTOM_PICKUP_TEST_PLACEMENTS")


def __getattr__(name: str):
    if name in _CONTENT_NAMES:
        from . import custom_pickup_content
        return getattr(custom_pickup_content, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# --- Inventory text-id allocation (shared with inventory_menu.py) ---
# New name/description strings are appended to the relocated sUnk_08506B38 string-pointer array, which
# originally has STRINGPTR_COUNT entries; each inventory item gets two new text-ids (name, desc).
STRINGPTR_COUNT = 2895                  # entries in sUnk_08506B38 (0x08506B38)


def inventory_pickups() -> List[CustomPickup]:
    """Return the custom pickups that appear as Item-Use key items, in registry order.

    Each pickup's position in this list assigns its text-ids."""
    return [p for p in _registry() if p.inventory_name is not None]


def name_text_id(i: int) -> int:
    """Name string text-id for the i-th inventory item (index into the relocated string-ptr array)."""
    return STRINGPTR_COUNT + 2 * i


def desc_text_id(i: int) -> int:
    return STRINGPTR_COUNT + 2 * i + 1


def _name_text_id_for(pickup: CustomPickup) -> int:
    """The name text-id for a pickup (0 = behaviour-only, no inventory row / textbox)."""
    inv = inventory_pickups()
    return name_text_id(inv.index(pickup)) if pickup in inv else 0


def flag_location(pickup: CustomPickup) -> Tuple[int, int]:
    """``(EWRAM byte offset, bit)`` of the flag ``pickup`` sets on collection. ``flag_number`` is a
    bit index into the ``flag_field`` array, so #48 in the MISC field (0x344) is byte 0x34A, bit 0."""
    return pickup.flag_field + pickup.flag_number // 8, pickup.flag_number % 8


def announcement_for_flag(flag_offset: int, flag_bit: int) -> Optional[Tuple[int, int]]:
    """Return ``(name text-id, sfx)`` for the inventory pickup that sets this flag bit, or None.

    A flag-only AP item received from another world sets the same flag that collecting its floor
    copy sets. This lookup lets the client announce it with the floor copy's name and sound."""
    for pickup in _registry():
        if pickup.inventory_name is not None and flag_location(pickup) == (flag_offset, flag_bit):
            return _name_text_id_for(pickup), pickup.sfx
    return None


def _icon_tile_file_offsets(icon_id: int) -> tuple[int, int]:
    """File offsets for an icon's top and bottom 0x40-byte halves within its sheet (sub_0801232C)."""
    sheet = (icon_id - 1) >> 6
    k = (icon_id - 1) - (sheet << 6)
    within = ((k >> 3) << 10) + 4 + ((k & 7) << 6)
    base = ICON_SHEETS_GBA[sheet] - GBA_ROM_BASE
    return base + within, base + within + 0x200


def _trampoline_bytes(target_gba: int) -> bytes:
    # ldr r3,[pc,#0]; bx r3; .word target|thumb  (r3 is dead at sub_080441F8's entry)
    return bytes.fromhex("004b1847") + struct.pack("<I", target_gba | 1)


def _desc_table(pickups: List[CustomPickup]) -> bytes:
    # 12-byte rows: var_b, flag_field, flag_number, sfx, name_text_id (0 = behaviour-only), reserved.
    rows = bytearray()
    for p in pickups:
        rows += struct.pack("<HHHHHH", p.item_offset, p.flag_field, p.flag_number, p.sfx,
                            _name_text_id_for(p), 0)
    rows += DESC_TERMINATOR + b"\x00\x00\x00\x00"   # pad terminator to the 12-byte stride
    return bytes(rows)


def menu_slot_bound() -> int:
    """Highest Item-Use slot the menu must iterate (= max inventory item_offset, else vanilla 31)."""
    return max((p.item_offset for p in inventory_pickups()), default=CONSUMABLE_COUNT - 1)


def _extended_icon_table(consumable_original_0x200: bytes, pickups: List[CustomPickup]) -> bytes:
    if len(consumable_original_0x200) != CONSUMABLE_COUNT * CONSUMABLE_ENTRY_SIZE:
        raise ValueError("consumable_original must be the 0x200-byte vanilla table")
    # Cover every slot the menu can iterate (0..bound) so no row reads past the table.
    n_entries = max(menu_slot_bound() + 1, max((p.item_offset for p in pickups), default=0) + 1)
    table = bytearray(consumable_original_0x200)
    table += bytes(n_entries * CONSUMABLE_ENTRY_SIZE - len(table))  # grow (zeroed) to fit customs
    for p in pickups:
        base = p.item_offset * CONSUMABLE_ENTRY_SIZE
        table[base + ITEM_ENTRY_GID_OFF] = p.item_offset & 0xFF   # gid == slot (indexes name/desc tables)
        table[base + ITEM_ENTRY_ICON_OFF] = p.icon_id
        table[base + ITEM_ENTRY_PAL_OFF] = ITEMS_PALETTE_BANK     # bank 6: the always-loaded items palette
        if p.inventory_name is not None:
            table[base + ITEM_ENTRY_USE_OFF] = USE_TYPE_KEYITEM   # shown in Item-Use, not usable
    return bytes(table)


def build_writes(consumable_table_original: bytes) -> Dict[int, bytes]:
    """``{rom_file_offset: bytes}`` installing the custom-pickup framework.

    ``consumable_table_original`` is the vanilla 0x200-byte consumable-icon table (file 0x505B3C of
    the base ROM), which :func:`build_writes_from_rom` slices out and passes in. rom/patch.py
    applies these writes like any other patch token, and encodes each location that spawns a
    custom pickup with ``get_encoding``.
    """
    writes: Dict[int, bytes] = {
        HOOK_SITE_GBA - GBA_ROM_BASE: _trampoline_bytes(CUSTOMHOOK_BASE_GBA),
        CUSTOMHOOK_BASE_GBA - GBA_ROM_BASE: CUSTOMHOOK_BLOB,
        CUSTOM_DESC_TABLE_GBA - GBA_ROM_BASE: _desc_table(_registry()),
        CUSTOM_ICON_TABLE_GBA - GBA_ROM_BASE: _extended_icon_table(consumable_table_original, _registry()),
        # Repoint the spawn-path consumable-icon literal to the extended table (existing items unaffected).
        CONSUMABLE_ICON_LITERAL_FILE_OFFSET: struct.pack("<I", CUSTOM_ICON_TABLE_GBA),
    }
    # build_writes_from_rom writes the icon TILES instead, because resolving an icon source needs
    # the full base ROM (a RomSprite is extracted from it).
    return writes


def _used_icon_ids(base_rom: bytes) -> set:
    """Every icon id (item-entry +2 byte) used by a real consumable/weapon/armor item in the base ROM.
    A custom icon must avoid these (its tiles overwrite that slot in the shared icon sheets)."""
    used = set()
    for base, stride, count in ((CONSUMABLE_TABLE_GBA, CONSUMABLE_ENTRY_SIZE, CONSUMABLE_COUNT),
                                (WEAPON_TABLE_GBA, WEAPON_ENTRY_SIZE, WEAPON_COUNT),
                                (ARMOR_TABLE_GBA, ARMOR_ENTRY_SIZE, ARMOR_COUNT)):
        off = base - GBA_ROM_BASE
        for i in range(count):
            used.add(base_rom[off + i * stride + ITEM_ENTRY_ICON_OFF])
    return used


def build_writes_from_rom(base_rom: bytes) -> Dict[int, bytes]:
    """Return :func:`build_writes` plus every pickup's icon tiles, all built from ``base_rom``.

    The vanilla consumable table is sliced out of ``base_rom``, and each icon source is resolved
    against the same ROM. Raises ValueError if a custom icon id belongs to a real consumable,
    weapon or armor item, because the custom tiles would replace that item's floor sprite. The
    0x1f..0x40 range is free in the vanilla ROM, but the check reads the actual tables rather than
    relying on that."""
    used = _used_icon_ids(base_rom)
    for p in _registry():
        if p.icon_id in used:
            raise ValueError(f"{p.name}: icon id {p.icon_id:#x} is used by a real item -- its tiles "
                             f"would corrupt that item's floor sprite; pick a free id (0x1f..0x40)")
    off = CONSUMABLE_TABLE_GBA - GBA_ROM_BASE
    writes = build_writes(base_rom[off:off + CONSUMABLE_COUNT * CONSUMABLE_ENTRY_SIZE])
    # Resolve each pickup's icon source to its 0x80-byte bank-6 tiles and write them into the icon
    # sheet. A RomSprite is extracted from the player's own base_rom (no copyrighted pixels ship);
    # an ImageFile is the modder's vendored original art. See rom/icon.py.
    for p in _registry():
        tiles = build_icon_tiles(base_rom, p.icon)
        top, bottom = _icon_tile_file_offsets(p.icon_id)
        writes[top] = tiles[:ICON_HALF]
        writes[bottom] = tiles[ICON_HALF:]
    return writes


# Pickup-entity encoding for a custom item: (type_num, subtype_num, item_offset), where type 4 is
# PICKUP, subtype 2 is consumable, and item_offset is the var_b that DESC_TABLE is keyed by.
# The subtype MUST stay 2, because only the subtype-2 spawn path's icon literal (file 0x440B4)
# points at the extended table. The weapon and armor paths (subtypes 3 and 4) and the by-global-id
# path (subtype 0xFF) read the ORIGINAL tables, so an item with var_b >= 32 on any of those
# subtypes would read past them or read the wrong entry.
def get_encoding(pickup: CustomPickup) -> tuple[int, int, int]:
    return (4, 2, pickup.item_offset)
