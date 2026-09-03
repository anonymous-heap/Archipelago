"""
Custom-pickup framework for Castlevania: Aria of Sorrow.

Adds *pickups that run custom on-collection behaviour* (set an arbitrary memory flag + play a
sound, grant no item) without touching any existing item identity. Extensible: each custom item is
one ``CustomPickup`` row; adding another is pure data (a descriptor row + a 16x16 icon + a room
placement). The first item is the "Forbidden Area button": collecting it sets MISC flag #48 -- the
exact flag the A01 object-0x35 press-button writes -- so the paired barrier sinks/opens, and plays
the button SFX (song 0x133). See ``custom_pickups.s`` for the full write-up.

How it fits together (all verified against the USA ROM):

* New item space, zero existing items touched. Custom items are consumables (subtype 2) at
  ``item_offset >= 32``. The consumable-icon lookup in the spawn path (sub_08044054) loads its table
  base from ONE literal at file 0x440B4 (= 0x08505B3C). We repoint that literal to an EXTENDED table
  in free ROM: the 32 original entries copied verbatim + our custom entries at offsets 32+. Normal
  consumables (0..31) read identical copies; the collect-grant / menu / shop paths still use the
  original table. So nothing the player can already get changes.

* Custom icons, no sheet relocation. Icon ids 0x1f..0x40 are unused (consumables end at 0x1e,
  weapons start at 0x41) -> 34 free 16x16 slots in icon sheet 0 (raw, DMA'd). Each custom entry's
  +2 byte points at one; we write the tiles straight into the slot. Each pickup declares an icon
  SOURCE (see rom/icon.py): a ``RomSprite`` extracted from the PLAYER's own ROM at build time (for art
  already in the game, e.g. the metal-gate-button -- so no copyrighted pixels ship, only an address +
  layout + palette-index remap), or an ``ImageFile`` (the modder's vendored ORIGINAL art). Both resolve
  to bank-6 tiles in build_writes_from_rom; the apworld itself contains no ROM-derived graphics.

* Behaviour, table-driven. The collect hook (custom_pickups.s, installed over sub_080441F8) gates on
  category==consumable, scans DESC_TABLE for the pickup's var_b, and on a hit sets the row's flag,
  plays its SFX, and jumps to the vanilla finish (writes the 0x360 "collected" flag from var_a,
  returns) -- granting nothing. Misses resume vanilla collection untouched.

* Palette: custom icons reuse OBJ palette bank 6 -- THE shared items/pickup palette, loaded for
  every floor pickup in every room (so it is never clobbered). Bank 6 is items-palette sub-palette 2:
  the loader sub_0800C5D8 does ``sub_0803C7B4(0x082099FC, sUnk_084F0DD8[bank], 1, bank)``, copying
  from ``0x082099FC + 4 + sub*0x20`` (a 4-byte header precedes the data), and sUnk_084F0DD8[6] == 2.
  That matches DSVEdit (an item's packed "Icon" field is ``(palette_bank<<8)|icon``, and it renders
  with sub-palette ``bank-4`` = 2). Sub-palette 2 is a 16-colour rainbow (gold #F8D848, tan #F8B070,
  orange/red, blues/greens, greys #A0A0A8/#E8D8D0, near-black), so most icons can be expressed in it
  directly -- quantise the tiles against BANK6_PALETTE (below). No per-item palette is written; the
  item entry's +3 byte stays bank 6. (An earlier design wrote a per-item palette into an unused
  sub-palette + a spare OBJ bank 10-15, but those banks are reused by enemy/effect sprites, so the
  colours broke in busy rooms; bank 6 is reliable. See git history if a truly out-of-gamut icon ever
  needs its own palette.)

Gating lives with the caller: rom/patch.py emits these writes (and encodes a location's pickup as a
custom item) when the relevant option/placement is active. This module just builds the bytes.

The blob is assembled from custom_pickups.s, beside this file. It is
position-DEPENDENT: the literal pool bakes absolute addresses, so relinking to a different
address means re-assembling and keeping the .s's DESC_TABLE in step with
CUSTOM_DESC_TABLE_GBA below. Rebuild with:

    arm-none-eabi-as -mcpu=arm7tdmi -mthumb custom_pickups.s -o /tmp/c.o
    arm-none-eabi-ld -Ttext=0x08670300 /tmp/c.o -o /tmp/c.elf
    arm-none-eabi-objcopy -O binary /tmp/c.elf /tmp/c.bin   # -> CUSTOMHOOK_BLOB
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
# Verified in the USA ROM: consumable icon bytes 0x01..0x1e, weapon 0x41..0x7a, armor 0x81..0xb4 --
# so 0x1f..0x40 is genuinely free (the spawn icon byte is each entry's +2, same as consumables).
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

# Dispatcher blob, assembled from custom_pickups.s at -Ttext=0x08670300 (THUMB, ARMv4T).
# Position-DEPENDENT: its literal pool bakes CUSTOM_DESC_TABLE_GBA (0x08670400), gEwramData
# (0x02000000), PlaySong+1 (0x080d7911), and the vanilla resume/finish addresses, so keep
# CUSTOMHOOK_BASE_GBA / CUSTOM_DESC_TABLE_GBA in sync if you relink.
CUSTOMHOOK_BLOB = bytes.fromhex(
    # literal pool bakes 0x08670400 (desc table), 0x02000000 (gEwram), 0x0800ef99 (sub_0800EF98|1,
    # got-item textbox), 0x080d7911 (PlaySong|1), 0x08044203/0x08044509. (No shadow-inventory literal:
    # the collect hook no longer writes the menu shadow -- owned-state is derived from the flag.)
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
# struct (struct ends at 0x02025554; EWRAM ends 0x02040000 -- the whole tail is untouched by game code
# after the boot zero-fill). It is NOT SRAM-saved:
# the menu trampoline rebuilds it on every build/recount -- slots 0..31 mirror the real consumables,
# and each custom slot is re-derived from that item's saved behaviour flag (the DESC_TABLE row's
# flag_field/flag_number), so the saved flag is the single source of truth.
# (The whole of pad_133A0 0x133A0..0x133EB is soul-system storage -- bestiary bitfield 0x133A0..0x133BF
#  + equip/sorted-soul lists 0x133D0..0x133E7 -- so it MUST NOT be used; that was a two-way save-corrupt
#  bug. See inventory_menu.s.)
SHADOW_INVENTORY_GBA = 0x02030000       # transient; gEwramData+0x30000, far above the struct (0x25554)
SHADOW_BASE_GBA = 0x0202FFC8            # base s.t. base+0x38 == SHADOW_INVENTORY_GBA
MAX_SHADOW_SLOT = 0x2B                  # 44-slot shadow (0x02030000..0x0203002B); ample free space here
USE_TYPE_KEYITEM = 0x04                 # item-table +8: >=4 => shown but not usable (like Castle Maps)
ITEM_ENTRY_USE_OFF = 0x8
ITEM_ENTRY_GID_OFF = 0x0


@dataclass(frozen=True)
class CustomPickup:
    """One custom pickup. ``item_offset`` (var_b) must be >= 32 and unique; ``icon_id`` a free id
    in 0x1f..0x40 (unique per item). ``icon_tiles`` is the 0x80-byte blob from tools/png_to_icon.py.
    On collection the hook sets flag ``flag_number`` in ``flag_field`` and plays ``sfx`` (0 = silent),
    despawns the floating pickup, and marks the pickup collected (its var_a 0x360 flag).

    If ``inventory_name`` is set, it is also a Castle-Map-style key item: it occupies Item-Use slot
    ``item_offset`` (shown, non-usable), with ``inventory_name`` for the "Got <name>" textbox + the
    menu row and ``description`` (<=2 lines, <=34 chars each) for the detail panel. Requires the
    inventory_menu extension. When ``inventory_name`` is None the pickup is behaviour-only (no row)."""
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
            # vanilla item names go to ~14 chars; longer is allowed but may crowd the count column.
            raise ValueError(f"{self.name}: inventory_name must be <= 18 chars")
        if self.description is not None and (len(self.description) > 2
                                             or any(len(line) > 34 for line in self.description)):
            raise ValueError(f"{self.name}: description must be <= 2 lines of <= 34 chars")


# --- Registry access (CONTENT lives in custom_pickup_content.py) ------------------------------------
# To ADD A PICKUP you edit custom_pickup_content.py, not this file. The machinery here pulls that
# registry LAZILY, so the two modules stay decoupled without an import cycle (content imports this
# machinery at load; this machinery touches content only at call time).
def _registry() -> List[CustomPickup]:
    from . import custom_pickup_content
    return custom_pickup_content.CUSTOM_PICKUPS


def validate_registry(pickups: List[CustomPickup]) -> None:
    """Fail loudly on colliding slots/icons. item_offset doubles as the shadow slot and the gid
    (name/desc index); icon_id is the shared icon-sheet slot -- duplicates would silently shadow a row
    or overwrite a sprite. custom_pickup_content calls this after building its CUSTOM_PICKUPS list."""
    offsets = [p.item_offset for p in pickups]
    icons = [p.icon_id for p in pickups]
    if len(set(offsets)) != len(offsets):
        raise ValueError(f"duplicate custom-pickup item_offset(s): {sorted(offsets)}")
    if len(set(icons)) != len(icons):
        raise ValueError(f"duplicate custom-pickup icon_id(s): {[hex(i) for i in sorted(icons)]}")


# Re-export the content registry's names so existing ``custom_pickups.CUSTOM_PICKUPS`` /
# ``.STUDY_SEALSWITCH`` / ``.FORBIDDEN_AREA_BUTTON`` / ``.CUSTOM_PICKUP_TEST_PLACEMENTS`` references
# (tests, patch.py) keep working after the content moved out (PEP 562 module-level __getattr__).
_CONTENT_NAMES = ("CUSTOM_PICKUPS", "STUDY_SEALSWITCH", "FORBIDDEN_AREA_BUTTON", "CUSTOM_PICKUP_TEST_PLACEMENTS")


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
    """Custom pickups that appear as Item-Use key items, in stable order (defines their text-ids)."""
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
    the base ROM); rom/patch.py has the base ROM and passes it in. Caller writes these like any other
    patch token, and encodes the chosen location's pickup as a custom item via ``get_encoding``.
    """
    writes: Dict[int, bytes] = {
        HOOK_SITE_GBA - GBA_ROM_BASE: _trampoline_bytes(CUSTOMHOOK_BASE_GBA),
        CUSTOMHOOK_BASE_GBA - GBA_ROM_BASE: CUSTOMHOOK_BLOB,
        CUSTOM_DESC_TABLE_GBA - GBA_ROM_BASE: _desc_table(_registry()),
        CUSTOM_ICON_TABLE_GBA - GBA_ROM_BASE: _extended_icon_table(consumable_table_original, _registry()),
        # Repoint the spawn-path consumable-icon literal to the extended table (existing items unaffected).
        CONSUMABLE_ICON_LITERAL_FILE_OFFSET: struct.pack("<I", CUSTOM_ICON_TABLE_GBA),
    }
    # Note: the icon TILES are written by build_writes_from_rom -- each pickup's icon source
    # (RomSprite / ImageFile) needs the full base ROM to resolve (a RomSprite is extracted from it).
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
    """Convenience: slice the vanilla consumable table out of the full base ROM and build the writes.
    Also a build-time guard: confirm no custom icon id collides with a real item's icon (the
    0x1f..0x40 range is free in the vanilla ROM, but scanning the actual tables future-proofs it)."""
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


# Pickup-entity encoding for a custom item: (type_num, subtype_num, item_offset). type 4 = PICKUP,
# subtype 2 = consumable. subtype MUST stay 2: only the subtype-2 spawn-path icon literal (file
# 0x440B4) is repointed to our extended table; the subtype-3/4 (weapon/armor) and subtype-0xFF
# (by-global-id) spawn paths read the ORIGINAL tables, so a var_b>=32 item spawned on any other
# subtype would read past / mis-read them. item_offset = var_b key.
def get_encoding(pickup: CustomPickup) -> tuple[int, int, int]:
    return (4, 2, pickup.item_offset)

# (The pickup registry and dev test placements -- the editable "content" -- live in
# custom_pickup_content.py; ``custom_pickups.CUSTOM_PICKUPS`` etc. re-export them via __getattr__.)
