"""
ROM patching for Castlevania: Aria of Sorrow.
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Dict, List

from BaseClasses import ItemClassification
from settings import get_settings
from worlds.Files import APPatchExtension, APProcedurePatch, APTokenMixin, APTokenTypes

from ..data import pickup_info_collection as pickup_infos
from ..constants import USA_ROM_MD5
from ..items import FORBIDDEN_AREA_SWITCH, item_table
from . import (classicvania_movement, custom_pickups, deathlink_hook, forbidden_area_button,
               inventory_menu, oops_all_whips, single_jump_divekick, skull_key_warp,
               soul_drop_rates)
from .entity import GBA_ROM_BASE
from ..options import ForbiddenAreaButton

if TYPE_CHECKING:
    from BaseClasses import Location
    from .. import CVAOSWorld

# Archipelago metadata written into clean ROM free space. These are
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
# an ASM hook on the collect path, which is not written yet. Keep type=4 so the entity still
# sets its save flag.
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

    @staticmethod
    def apply_rom_features(caller: APProcedurePatch, rom: bytes, config_file_name: str) -> bytes:
        """
        Apply the ROM-reading feature patches at patch-apply time (non-host side)
        """

        config = json.loads(caller.get_file(config_file_name).decode("utf-8"))
        working = bytearray(rom)

        def apply(writes: Dict[int, bytes]) -> None:
            for offset, data in writes.items():
                working[offset:offset + len(data)] = data

        # Skull Key -> warp consumable hook (rom/skull_key_warp.py).
        if config["skull_key_warp"]:
            apply(skull_key_warp.build_writes())

        # Forbidden Area gate-unlock pickup toggle (replaces the A01 press-button
        # with a candle and a shuffled Study Sealswitch).
        if config["forbidden_area_pickup"]:
            apply(forbidden_area_button.build_writes(bytes(working)))

        # Adjust selected enemy soul-drop rates (rom/soul_drop_rates.py).
        apply(soul_drop_rates.build_writes(bytes(working)))

        # Gameplay tweaks ported from Xanthus's AoS patches (see each module's docstring).
        if config["single_jump_divekick"]:
            apply(single_jump_divekick.build_writes(bytes(working)))
        if config["classicvania_movement"]:
            apply(classicvania_movement.build_writes(bytes(working)))
        if config["oops_all_whips"]:
            apply(oops_all_whips.build_writes(bytes(working)))

        # Custom-pickup framework (rom/custom_pickups.py): collect hook, extended consumable-icon
        # table, and the apply icons.
        apply(custom_pickups.build_writes_from_rom(bytes(working)))

        # Item-Use menu extension (rom/inventory_menu.py): relocated name/desc/string tables built
        # from the current rom state, so all repoints above are included.
        apply(inventory_menu.build_writes(bytes(working)))

        return bytes(working)


class CVAOSProcedurePatch(APProcedurePatch, APTokenMixin):
    hash = [USA_ROM_MD5]
    patch_file_ending: str = ".apcvaos"
    result_file_ending: str = ".gba"
    game = "Castlevania - Aria of Sorrow"

    procedure = [
        ("apply_tokens", ["token_data.bin"]),
        ("apply_rom_features", ["rom_config.json"]),
    ]

    @classmethod
    def get_source_data(cls) -> bytes:
        return get_base_rom_bytes()


def patch_rom(world: CVAOSWorld, patch: CVAOSProcedurePatch, offset_data: Dict[int, bytes]) -> None:
    """Fill the patch container. ROM-free: tokens for everything derivable without the base ROM,
    plus ``rom_config.json`` telling ``apply_rom_features`` (patch-apply time, on the player's
    machine) which ROM-reading features this seed enabled."""
    for offset, data in offset_data.items():
        patch.write_token(APTokenTypes.WRITE, offset, data)

    # AP metadata in ROM free space: the identifier the client validates, and the
    # slot auth it reads to connect.
    patch.write_token(APTokenTypes.WRITE, ARCHIPELAGO_IDENTIFIER_START,
                      ARCHIPELAGO_IDENTIFIER.encode("ascii"))
    patch.write_token(APTokenTypes.WRITE, AUTH_NUMBER_START, bytes(world.auth))

    # DeathLink "real kill" hook (the "Death Link" option; see rom/deathlink_hook.py). A fixed
    # blob + veneer needing no ROM reads, so it can stay a plain token.
    if world.options.death_link:
        for offset, data in deathlink_hook.build_writes().items():
            patch.write_token(APTokenTypes.WRITE, offset, data)

    # Everything that must READ the base ROM (guards, relocated tables, icon extraction) is
    # deferred to apply_rom_features via this config. Keys must match what it looks up.
    rom_config = {
        "skull_key_warp": bool(world.options.skull_key_warp),
        "forbidden_area_pickup":
            world.options.forbidden_area_button.value == ForbiddenAreaButton.option_pickup,
        "single_jump_divekick": bool(world.options.single_jump_divekick),
        "classicvania_movement": bool(world.options.classicvania_movement),
        "oops_all_whips": bool(world.options.oops_all_whips),
    }
    patch.write_file("rom_config.json", json.dumps(rom_config).encode("utf-8"))

    patch.write_file("token_data.bin", patch.get_token_binary())
