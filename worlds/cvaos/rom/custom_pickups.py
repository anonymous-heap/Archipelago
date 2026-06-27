"""
Custom-pickup framework for Castlevania: Aria of Sorrow.

Adds *pickups that run custom on-collection behaviour* (set an arbitrary memory flag + play a
sound, grant no item) without touching any existing item identity. Extensible: each custom item is
one ``CustomPickup`` row; adding another is pure data (a descriptor row + a 16x16 icon + a room
placement). The first item is the "Forbidden Area button": collecting it sets MISC flag #48 -- the
exact flag the A01 object-0x35 press-button writes -- so the paired barrier sinks/opens, and plays
the button SFX (song 0x133). See ``custom_pickups.s`` for the full decomp write-up.

How it fits together (all verified against cvaos-decomp + the USA ROM):

* New item space, zero existing items touched. Custom items are consumables (subtype 2) at
  ``item_offset >= 32``. The consumable-icon lookup in the spawn path (sub_08044054) loads its table
  base from ONE literal at file 0x440B4 (= 0x08505B3C). We repoint that literal to an EXTENDED table
  in free ROM: the 32 original entries copied verbatim + our custom entries at offsets 32+. Normal
  consumables (0..31) read identical copies; the collect-grant / menu / shop paths still use the
  original table. So nothing the player can already get changes.

* Custom icons, no sheet relocation. Icon ids 0x1f..0x40 are unused (consumables end at 0x1e,
  weapons start at 0x41) -> 34 free 16x16 slots in icon sheet 0 (raw, DMA'd). Each custom entry's
  +2 byte points at one; we write the tiles straight into the slot.

* Behaviour, table-driven. The collect hook (custom_pickups.s, installed over sub_080441F8) gates on
  category==consumable, scans DESC_TABLE for the pickup's var_b, and on a hit sets the row's flag,
  plays its SFX, and jumps to the vanilla finish (writes the 0x360 "collected" flag from var_a,
  returns) -- granting nothing. Misses resume vanilla collection untouched.

* Palette: custom icons reuse OBJ palette bank 6 (the shared items palette), so tiles must be
  recoloured to bank 6's 16 colours. Use tools/dump_obj_palette.lua to dump bank 6, then
  tools/png_to_icon.py --palette to regenerate ICON_TILES. (The bundled tiles below are PROVISIONAL
  -- correct shape, placeholder colours -- until re-generated against the real bank 6.)

Gating lives with the caller: rom/patch.py emits these writes (and encodes a location's pickup as a
custom item) when the relevant option/placement is active. This module just builds the bytes.

Regenerate the hook blob after editing custom_pickups.s:
    arm-none-eabi-as -mcpu=arm7tdmi -mthumb custom_pickups.s -o /tmp/c.o
    arm-none-eabi-ld -Ttext=0x08660300 /tmp/c.o -o /tmp/c.elf
    arm-none-eabi-objcopy -O binary /tmp/c.elf /tmp/c.bin   # -> CUSTOMHOOK_BLOB
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .entity import GBA_ROM_BASE

# --- Hook site: the pickup on-collision/collect callback sub_080441F8 ---
# We overwrite its first 8 bytes (push {r4-r7,lr}; mov r7,r8; push {r7}; sub sp,#4), which the hook
# replicates. 0x080441F8 is word-aligned so the ldr/bx/.word veneer's [pc,#0] reads the target word.
HOOK_SITE_GBA = 0x080441F8

# --- Free-ROM placement (GBA addresses; clear of identifier/auth/deathlink/skull-key at 0x660000..0x66026F) ---
CUSTOMHOOK_BASE_GBA = 0x08660300        # the dispatcher blob
CUSTOM_DESC_TABLE_GBA = 0x08660400      # descriptor table (baked into the blob's literal pool)
CUSTOM_ICON_TABLE_GBA = 0x08661000      # extended consumable-icon table (repoint target)

# --- Vanilla consumable-icon table (the spawn-path lookup we extend) ---
CONSUMABLE_TABLE_GBA = 0x08505B3C
CONSUMABLE_ENTRY_SIZE = 0x10
CONSUMABLE_COUNT = 32                    # entries 0..31; table is 0x200 bytes
CONSUMABLE_ICON_LITERAL_FILE_OFFSET = 0x440B4   # `ldr r0,=0x08505B3C` in sub_08044054 (subtype-2 branch)
ITEM_ENTRY_ICON_OFF = 0x2               # +2 = icon id, +3 = OBJ palette bank
ITEM_ENTRY_PAL_OFF = 0x3
ITEMS_PALETTE_BANK = 6                   # shared item-pickup OBJ palette

# --- Common-icon sheets (sub_0801232C); a 16x16 icon = top 0x40 @ off, bottom 0x40 @ off+0x200 ---
ICON_SHEETS_GBA = (0x081C5E00, 0x081C7E04, 0x081C9E08)   # 64 icons/sheet
ICON_HALF = 0x40

# Dispatcher blob, assembled from custom_pickups.s at -Ttext=0x08660300 (THUMB, ARMv4T).
# Position-DEPENDENT: its literal pool bakes CUSTOM_DESC_TABLE_GBA (0x08660400), gEwramData
# (0x02000000), PlaySong+1 (0x080d7911), and the vanilla resume/finish addresses, so keep
# CUSTOMHOOK_BASE_GBA / CUSTOM_DESC_TABLE_GBA in sync if you relink.
CUSTOMHOOK_BLOB = bytes.fromhex(
    # literal pool bakes 0x08660400 (desc table), 0x02000000 (gEwram), 0x020133a0 (shadow inv),
    # 0x0800ef99 (sub_0800EF98|1, got-item textbox), 0x080d7911 (PlaySong|1), 0x08044203/0x08044509.
    "f0b5474680b481b0041c3620205c022809d1658e184e30881849884203d0a84204d00c36f7e7201c154b18473089002805d0144901224a55134b00f01bf87188b2881248401853099b00c0181f21114001238b40016819430160f088002802d00b4b00f007f85920215c082211432154084b18471847c04600046608ffff000003420408a033010299ef00080000000211790d0809450408"
)
assert len(CUSTOMHOOK_BLOB) == 152, f"hook blob must be 152 bytes, got {len(CUSTOMHOOK_BLOB)}"
assert CUSTOM_DESC_TABLE_GBA.to_bytes(4, "little") in CUSTOMHOOK_BLOB, "desc-table addr missing from blob"

# Flag-field byte offsets from gEwramData (0x02000000). The hook computes
#   dword = gEwram + field + (flag_number>>5)*4 ;  *dword |= 1 << (flag_number & 0x1F)
FLAG_FIELD_EVENT = 0x33C     # SetEventFlag field
FLAG_FIELD_MISC = 0x344      # the A01 button's field
FLAG_FIELD_PICKUP = 0x360    # per-location "collected" field
FLAG_FIELD_BOSS = 0x37E      # boss-death field

DESC_TERMINATOR = b"\xff\xff\x00\x00\x00\x00\x00\x00"


# The Item-Use shadow inventory lives in pad_133A0 (gEwramData+0x133A0), slots 0..MAX_SHADOW_SLOT.
# Slots 0..31 mirror the real consumables (synced by the menu trampoline); 32..MAX are new key items.
SHADOW_INVENTORY_GBA = 0x020133A0
SHADOW_BASE_GBA = 0x02013368            # base s.t. base+0x38 == SHADOW_INVENTORY_GBA
MAX_SHADOW_SLOT = 0x4B                  # pad_133A0 is 76 bytes (0..0x4B)
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
    icon_tiles: bytes
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
        if len(self.icon_tiles) != 2 * ICON_HALF:
            raise ValueError(f"{self.name}: icon_tiles must be {2 * ICON_HALF} bytes")
        if self.inventory_name is not None and len(self.inventory_name) > 18:
            # vanilla item names go to ~14 chars; longer is allowed but may crowd the count column.
            raise ValueError(f"{self.name}: inventory_name must be <= 18 chars")
        if self.description is not None and (len(self.description) > 2
                                             or any(len(line) > 34 for line in self.description)):
            raise ValueError(f"{self.name}: description must be <= 2 lines of <= 34 chars")


# Provisional button tiles (correct shape; placeholder colours). Regenerate against bank 6:
#   python tools/png_to_icon.py button_pickup.png --palette "<dump_obj_palette.lua output>"
_BUTTON_TILES = bytes.fromhex(
    "00000000000000001021323311224365112243751122437510112233000098ba00000000000000003323120157342211573422115734221133221101ac890000"   # top  (tiles 0,1)
    "00000098000000a9000000ca000000ba000000ca000000a9000000000000000088000000890000009a0000009a0000009a000000990000000000000000000000"   # bottom (tiles 2,3)
)

# --- Registry of custom pickups (add a row to add a pickup) ---
STUDY_SEALSWITCH = CustomPickup(
    name="Study Sealswitch",
    item_offset=32,                # first custom / Item-Use slot (32..0x4B)
    icon_id=0x1F,                  # first free icon id (sheet 0) -- the floor pickup sprite
    icon_tiles=_BUTTON_TILES,
    flag_field=FLAG_FIELD_MISC,    # the A01 button writes the MISC field
    flag_number=48,                # misc flag #48 (0x02000348 bit 16) -> barrier sinks
    sfx=0x133,                     # the button's SFX/song
    inventory_name="Study Sealswitch",
    description=("The Study's underground egress was",
                 "supposed to be forever sealed..."),
)

CUSTOM_PICKUPS: List[CustomPickup] = [STUDY_SEALSWITCH]

# Backwards-compat alias (older references).
FORBIDDEN_AREA_BUTTON = STUDY_SEALSWITCH

# --- Inventory text-id allocation (shared with inventory_menu.py) ---
# New name/description strings are appended to the relocated sUnk_08506B38 string-pointer array, which
# originally has STRINGPTR_COUNT entries; each inventory item gets two new text-ids (name, desc).
STRINGPTR_COUNT = 2895                  # entries in sUnk_08506B38 (0x08506B38)


def inventory_pickups() -> List[CustomPickup]:
    """Custom pickups that appear as Item-Use key items, in stable order (defines their text-ids)."""
    return [p for p in CUSTOM_PICKUPS if p.inventory_name is not None]


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
        table[base + ITEM_ENTRY_PAL_OFF] = ITEMS_PALETTE_BANK
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
        CUSTOM_DESC_TABLE_GBA - GBA_ROM_BASE: _desc_table(CUSTOM_PICKUPS),
        CUSTOM_ICON_TABLE_GBA - GBA_ROM_BASE: _extended_icon_table(consumable_table_original, CUSTOM_PICKUPS),
        # Repoint the spawn-path consumable-icon literal to the extended table (existing items unaffected).
        CONSUMABLE_ICON_LITERAL_FILE_OFFSET: struct.pack("<I", CUSTOM_ICON_TABLE_GBA),
    }
    for p in CUSTOM_PICKUPS:
        top, bottom = _icon_tile_file_offsets(p.icon_id)
        writes[top] = p.icon_tiles[:ICON_HALF]
        writes[bottom] = p.icon_tiles[ICON_HALF:]
    return writes


def build_writes_from_rom(base_rom: bytes) -> Dict[int, bytes]:
    """Convenience: slice the vanilla consumable table out of the full base ROM and build the writes."""
    off = CONSUMABLE_TABLE_GBA - GBA_ROM_BASE
    return build_writes(base_rom[off:off + CONSUMABLE_COUNT * CONSUMABLE_ENTRY_SIZE])


# Pickup-entity encoding for a custom item: (type_num, subtype_num, item_offset). type 4 = PICKUP,
# subtype 2 = consumable (so spawn reads our extended icon table); item_offset = var_b key.
def get_encoding(pickup: CustomPickup) -> tuple[int, int, int]:
    return (4, 2, pickup.item_offset)


# --- Test placements (dev): map a location's display_name -> a CustomPickup to force it to spawn
# that custom pickup instead of its rolled item. Empty by default (the framework is inert without a
# placement). To try the button in-game, e.g.:  {"Lucky Charm": FORBIDDEN_AREA_BUTTON}
CUSTOM_PICKUP_TEST_PLACEMENTS: Dict[str, CustomPickup] = {}
