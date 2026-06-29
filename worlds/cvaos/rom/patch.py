"""
ROM patching for Castlevania: Aria of Sorrow.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Dict, List

from BaseClasses import ItemClassification
from settings import get_settings
from worlds.Files import APPatchExtension, APProcedurePatch, APTokenMixin, APTokenTypes

from ..data.pickup_info import rows as pickup_infos
from ..items import FORBIDDEN_AREA_SWITCH, item_table
from . import (custom_pickups, deathlink_hook, forbidden_area_button, inventory_menu,
               skull_key_warp)
from .entity import GBA_ROM_BASE
from ..options import ForbiddenAreaButton

if TYPE_CHECKING:
    from BaseClasses import Location
    from .. import CVAOSWorld

CVAOS_USA_HASH = "e7470df4d241f73060d14437011b90ce"

# Archipelago metadata written into clean ROM free space (CLIENT_PLAN sec. 4B/5c). These are
# *file offsets* — both APProcedurePatch tokens and the BizHawk ROM domain the client reads
# are file-offset based. The region at file 0x660000+ (GBA 0x08660000) is well clear of the
# last real data at 0x651163. ARCHIPELAGO_IDENTIFIER doubles as the client/patch compatibility
# gate; bump it whenever the patch/client contract changes.
ARCHIPELAGO_IDENTIFIER_START = 0x660000   # 13 bytes
ARCHIPELAGO_IDENTIFIER = "CVAOS_AP_V0.2"
AUTH_NUMBER_START = 0x660010              # 16 bytes


# Item encoding lookup: AP item *code* -> (type_num, subtype_num, item_offset). Keyed by the stable
# packed code (not the display name) so renaming AP items can never desync ROM item placement.

_item_encoding: Dict[int, tuple[int, int, int]] = {
    item_table[p.display_name].code: (p.type_num, p.subtype_num, p.item_offset)
    for p in pickup_infos
}


def get_item_encoding(item_code: int) -> tuple[int, int, int]:
    """Return (type_num, subtype_num, item_offset) for a CVAoS item by its packed AP code."""
    return _item_encoding[item_code]


# Placeholder appearance for locations holding another world's item. AoS must physically give the
# collector *something* when a pickup is collected, and the data has no "null item", so we use a
# Skull Key (PICKUP type 4, consumable subtype 2, item_offset 25): an item not available in the game
# (and therefore an item that is *not* a placed)
# pickup anywhere, so it reads as an obvious "this was someone else's item" token.\
# This is intentional: the AP location check is driven by the collected-pickup save flag (the client reads
# PICKUP_FLAGS), NOT by what the pickup grants, so the substitute never affects check-sending. The
# real behaviour -- grant nothing locally and show a "sent X to Player Y" multiworld textbox -- needs
# the Phase 6 Strategy B ASM hook (see ROADMAP). Keep type=4 so the entity still sets its save flag.
_AP_PLACEHOLDER = (4, 2, 25)  # Skull Key

# Synthetic items (no ground pickup) that appear in the local world as a custom behaviour-pickup.
# The Study Sealswitch AP item spawns the in-game Study Sealswitch custom pickup, which sets misc #48
# on collection -- the same flag its FLAG_ONLY receive path sets, so the unlock works whether collected
# locally or received from elsewhere. Keyed by item display name (codes are packed, not name-derived).
_ITEM_CUSTOM_PICKUP = {FORBIDDEN_AREA_SWITCH: custom_pickups.STUDY_SEALSWITCH}

# Location data lookup: Location number -> ROM bytes

def get_location_data(world: CVAOSWorld, active_locations: List[Location]) -> Dict[int, bytes]:
    """Build a dict of {rom_file_offset: bytes_to_write} for every location."""
    writes: Dict[int, bytes] = {}

    for loc in active_locations:
        rom_offset = loc.address - GBA_ROM_BASE

        custom = custom_pickups.CUSTOM_PICKUP_TEST_PLACEMENTS.get(loc.name)
        if custom is None and loc.item.player == world.player:
            # The local player's shuffled synthetic items (e.g. the Study Sealswitch) spawn their
            # real in-game custom pickup. A foreign player's copy stays a placeholder and is delivered
            # through its FLAG_ONLY receive path.
            custom = _ITEM_CUSTOM_PICKUP.get(loc.item.name)
        if custom is not None:
            type_num, subtype_num, item_offset = custom_pickups.get_encoding(custom)
        elif loc.item.player == world.player:
            type_num, subtype_num, item_offset = get_item_encoding(loc.item.code)
        else:
            type_num, subtype_num, item_offset = _AP_PLACEHOLDER

        # Write type + subtype at entity +0x05
        writes[rom_offset + 0x05] = bytes([type_num, subtype_num])
        # Write item_offset at entity +0x0A  (little-endian u16)
        writes[rom_offset + 0x0A] = item_offset.to_bytes(2, "little")

    return writes


# Patch classes

def get_base_rom_bytes() -> bytes:
    file_name = get_settings().cvaos_options.rom_file
    with open(file_name, "rb") as fh:
        return fh.read()


class CVAOSPatchExtension(APPatchExtension):
    game = "Castlevania - Aria of Sorrow"


class CVAOSProcedurePatch(APProcedurePatch, APTokenMixin):
    hash = [CVAOS_USA_HASH]
    patch_file_ending: str = ".apcvaos"
    result_file_ending: str = ".gba"
    game = "Castlevania - Aria of Sorrow"

    procedure = [
        ("apply_tokens", ["token_data.bin"]),
    ]

    @classmethod
    def get_source_data(cls) -> bytes:
        return get_base_rom_bytes()


def patch_rom(world: CVAOSWorld, patch: CVAOSProcedurePatch, offset_data: Dict[int, bytes]) -> None:
    """Write all item placement tokens into the patch."""
    for offset, data in offset_data.items():
        patch.write_token(APTokenTypes.WRITE, offset, data)

    # AP metadata in ROM free space: the identifier the client validates, and the
    # slot auth it reads to connect.
    patch.write_token(APTokenTypes.WRITE, ARCHIPELAGO_IDENTIFIER_START,
                      ARCHIPELAGO_IDENTIFIER.encode("ascii"))
    patch.write_token(APTokenTypes.WRITE, AUTH_NUMBER_START, bytes(world.auth))

    base_rom = get_base_rom_bytes()
    # Working copy that accumulates any writes which REPOINT entries in the name/desc text-id tables or
    # the string-pointer array. inventory_menu (below) RELOCATES those tables and repoints the text
    # resolver to its copies, so a repoint left only in the originals would be masked. Any feature that
    # edits those tables must be applied here before inventory_menu reads it.
    working = bytearray(base_rom)

    # Skull Key -> warp consumable hook (the "Skull Key Warp" option; see rom/skull_key_warp.py). It
    # also repoints the Skull Key description, so its writes must reach inventory_menu's relocated array.
    if world.options.skull_key_warp:
        for offset, data in skull_key_warp.build_writes().items():
            patch.write_token(APTokenTypes.WRITE, offset, data)
            working[offset:offset + len(data)] = data

    # DeathLink "real kill" hook (the "Death Link" option; see rom/deathlink_hook.py). Lets the
    # client trigger the game's actual death routine via a flag instead of zeroing HP.
    if world.options.death_link:
        for offset, data in deathlink_hook.build_writes().items():
            patch.write_token(APTokenTypes.WRITE, offset, data)

    # Forbidden Area "pickup" mode (rom/forbidden_area_button.py): replace the A01 press-button with an
    # inert candle so the barrier can only be opened by the shuffled Study Sealswitch. Barrier intact.
    if world.options.forbidden_area_button.value == ForbiddenAreaButton.option_pickup:
        for offset, data in forbidden_area_button.build_writes(base_rom).items():
            patch.write_token(APTokenTypes.WRITE, offset, data)

    # Custom-pickup framework (rom/custom_pickups.py): the collect hook, the extended consumable-icon
    # table (existing items unchanged), and the custom icons. Installed unconditionally -- it is inert
    # unless a location actually spawns a custom item (var_b >= 32), which only the test placements or
    # a future option do.
    for offset, data in custom_pickups.build_writes_from_rom(base_rom).items():
        patch.write_token(APTokenTypes.WRITE, offset, data)

    # Item-Use menu extension (rom/inventory_menu.py): shows custom "key items" in the pause Item-Use
    # list (name + description, non-usable) via a transient shadow inventory in free EWRAM. Reads
    # `working` so its relocated name/desc/string tables include the repoints applied above (e.g. the
    # Skull Key warp description). Emitted only when at least one custom pickup has an inventory_name.
    for offset, data in inventory_menu.build_writes(bytes(working)).items():
        patch.write_token(APTokenTypes.WRITE, offset, data)

    patch.write_file("token_data.bin", patch.get_token_binary())
