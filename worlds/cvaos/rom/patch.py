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
from ..constants import AC_USA_ROM_MD5, USA_ROM_MD5
from ..items import FORBIDDEN_AREA_SWITCH, item_table
from . import (classicvania_movement, custom_pickups, deathlink_hook, forbidden_area_button,
               inventory_menu, oops_all_whips, received_item_box, single_jump_divekick,
               skull_key_warp, soul_drop_rates, soul_guarantee_hook, soul_shuffle)
from .entity import GBA_ROM_BASE, AoSPickupEntity
from .._bytemaker_compat import offset_of
from ..options import ForbiddenAreaButton, SoulShuffle, TARGET_ADVANCE_COLLECTION, TARGET_GBA

if TYPE_CHECKING:
    from BaseClasses import Location
    from .. import CVAOSWorld

# Archipelago metadata written into clean ROM free space. These are
# *file offsets* — both APProcedurePatch tokens and the BizHawk ROM domain the client reads
# are file-offset based. The region at file 0x670000+ (GBA 0x08670000) is well clear of the
# last real cart data at 0x651163 and of the Advance Collection ROM's M2 additions
# (rom/address_space.py M2_NO_GO: 0x660000-0x6610BC and 0x700000-0x7000E3).
# ARCHIPELAGO_IDENTIFIER doubles as the client/patch compatibility gate; bump it whenever the
# patch/client contract changes.
ARCHIPELAGO_IDENTIFIER_START = 0x670000   # 13 bytes
ARCHIPELAGO_IDENTIFIER = "CVAOS_AP_V0.3"  # V0.3: hook/metadata block moved 0x660000 -> 0x670000
AUTH_NUMBER_START = 0x670010              # 16 bytes

# SoulShuffle option value -> rom/soul_shuffle mode.
_SOUL_SHUFFLE_MODES = {
    SoulShuffle.option_off: soul_shuffle.MODE_OFF,
    SoulShuffle.option_within_type: soul_shuffle.MODE_WITHIN_TYPE,
    SoulShuffle.option_any_type: soul_shuffle.MODE_ANY_TYPE,
}


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

# Where the two edited fields sit inside the 12-byte entity record, from the record's own
# layout rather than counted by hand. ``type`` and ``subtype`` are adjacent, so one write
# covers both.
_TYPE_OFF = offset_of(AoSPickupEntity, "type")
_ITEM_OFF = offset_of(AoSPickupEntity, "var_b")   # var_b carries the item_offset
assert offset_of(AoSPickupEntity, "subtype") == _TYPE_OFF + 1

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

        writes[rom_offset + _TYPE_OFF] = bytes([type_num, subtype_num])
        writes[rom_offset + _ITEM_OFF] = item_offset.to_bytes(2, "little")

    return writes


# Patch classes

def _read_base_rom_source(path: str) -> bytes:
    """Read a base ROM from *path*, which may be a GBA ROM or the collection's game.exe.

    A game.exe is detected by extension or PE (``MZ``) magic; in that case the stock AoS ROM is
    extracted from the ``windata/`` archive next to it (so the exe must be the real install file,
    not a copy). Anything else is returned as-is.
    """
    if path.lower().endswith(".exe"):
        from ..advance_collection.install import extract_base_rom_bytes
        return extract_base_rom_bytes(path)
    with open(path, "rb") as fh:
        head = fh.read(2)
        if head == b"MZ":  # a PE handed to us despite its name -> treat as the collection exe
            from ..advance_collection.install import extract_base_rom_bytes
            return extract_base_rom_bytes(path)
        return head + fh.read()


def get_base_rom_bytes(target: int = TARGET_GBA) -> bytes:
    """The base ROM to patch against, chosen by the seed's declared target platform.

    - ``advance_collection``: source the ROM from the installed collection automatically — the
      stock AoS ROM is pulled out of ``windata/`` next to ``collection_exe`` (default Steam path),
      so a collection player configures nothing. Only if the collection can't be found there does
      it fall back to the prompted ``rom_file``.
    - ``gba``: use ``rom_file``; accessing it opens a file browser when unset. The pick may be a
      GBA ROM *or* the collection's game.exe (``_read_base_rom_source`` extracts from the exe).
    """
    opts = get_settings().cvaos_options
    if target == TARGET_ADVANCE_COLLECTION:
        exe = str(opts.collection_exe)
        if exe and os.path.exists(exe):
            from ..advance_collection.install import extract_base_rom_bytes
            return extract_base_rom_bytes(exe)
    return _read_base_rom_source(str(opts.rom_file))


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

        # Ancient Book soul drops: the enemy-death roll hook (rom/soul_guarantee_hook.py). It checks
        # the instructions it replaces against the base ROM, which is why it is applied here.
        if config.get("ancient_book_soul_drops"):
            apply(soul_guarantee_hook.build_writes(bytes(working)))

        # Forbidden Area gate-unlock pickup toggle (replaces the A01 press-button
        # with a candle and a shuffled Study Sealswitch).
        if config["forbidden_area_pickup"]:
            apply(forbidden_area_button.build_writes(bytes(working)))

        # Reassign which enemy drops which soul (rom/soul_shuffle.py). Must precede
        # soul_drop_rates: it verifies the whole vanilla soul table, including the three rate
        # bytes soul_drop_rates then overwrites.
        soul_plan = {int(target): source
                     for target, source in config.get("soul_shuffle_plan", {}).items()}
        if soul_plan:
            apply(soul_shuffle.build_writes(bytes(working), soul_plan,
                                            config["keep_soul_drop_rates"],
                                            config["shuffle_starting_soul"]))

        # Adjust selected enemy soul-drop rates (rom/soul_drop_rates.py).
        apply(soul_drop_rates.build_writes(bytes(working)))

        # Global soul-drop-rate multiplier. Last of the rate edits on purpose: it reads the
        # rates as they now stand, so it composes with both the overrides above and a shuffle
        # that moved rates around.
        if config["multiply_soul_drop_rates"]:
            apply(soul_drop_rates.build_multiplier_writes(
                bytes(working), config["soul_drop_rate_multiplier"]))

        # Gameplay tweaks ported from Xanthus's AoS patches (see each module's docstring).
        if config["single_jump_divekick"]:
            apply(single_jump_divekick.build_writes(bytes(working)))
        if config["classicvania_movement"]:
            apply(classicvania_movement.build_writes(bytes(working)))
        if config["oops_all_whips"]:
            apply(oops_all_whips.build_writes(bytes(working)))

        # Received-item announcement (rom/received_item_box.py): the vanilla "Got <name>" textbox
        # and pickup SFX for an item or soul another world sent. Unconditional -- announcing what
        # you receive is not a gameplay option, and the writes are inert until the client posts a
        # request to the mailbox. Order-independent: it registers a slot in the shared
        # update-hook framework (rom/xanthus_framework.py), which is idempotent.
        apply(received_item_box.build_writes(bytes(working)))

        # Custom-pickup framework (rom/custom_pickups.py): collect hook, extended consumable-icon
        # table, and the apply icons.
        apply(custom_pickups.build_writes_from_rom(bytes(working)))

        # Item-Use menu extension (rom/inventory_menu.py): relocated name/desc/string tables built
        # from the current rom state, so all repoints above are included.
        apply(inventory_menu.build_writes(bytes(working)))

        return bytes(working)


class CVAOSProcedurePatch(APProcedurePatch, APTokenMixin):
    # Two accepted bases on purpose: the cart dump and the collection's own copy of the ROM,
    # which M2 altered (constants.py). The patch bytes are identical against either.
    hash = [USA_ROM_MD5, AC_USA_ROM_MD5]
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

    def get_source_data_with_cache(self) -> bytes:  # type: ignore[override]
        # Per-seed override of the base-class classmethod: the base ROM source depends on the
        # target platform the YAML declared, which patch_rom embedded in rom_config.json. Read it
        # here (files are loaded by patch() before this runs) so an advance_collection seed sources
        # the collection ROM and a gba seed prompts for a ROM/exe. No caching: single apply.
        try:
            target = int(json.loads(self.get_file("rom_config.json")).get("target_platform",
                                                                          TARGET_GBA))
        except (KeyError, ValueError, json.JSONDecodeError):
            target = TARGET_GBA
        return get_base_rom_bytes(target)


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
    # target_platform additionally drives base-ROM source selection at apply time
    # (get_source_data_with_cache); apply_rom_features ignores it.
    rom_config = {
        "target_platform": world.options.target_platform.value,
        "skull_key_warp": bool(world.options.skull_key_warp),
        "ancient_book_soul_drops": bool(world.options.ancient_book_soul_drops),
        "forbidden_area_pickup":
            world.options.forbidden_area_button.value == ForbiddenAreaButton.option_pickup,
        "single_jump_divekick": bool(world.options.single_jump_divekick),
        "classicvania_movement": bool(world.options.classicvania_movement),
        "oops_all_whips": bool(world.options.oops_all_whips),
        # Which enemy now drops which enemy's vanilla soul. Decided here (seeded, ROM-free --
        # soul_shuffle carries the vanilla table) and applied against the base ROM later.
        "soul_shuffle_plan": soul_shuffle.plan_shuffle(
            world.random, _SOUL_SHUFFLE_MODES[world.options.soul_shuffle.value]),
        "keep_soul_drop_rates": bool(world.options.keep_soul_drop_rates),
        "shuffle_starting_soul": bool(world.options.shuffle_starting_soul),
        "multiply_soul_drop_rates": bool(world.options.multiply_soul_drop_rates),
        "soul_drop_rate_multiplier": int(world.options.soul_drop_rate_multiplier.value),
    }
    patch.write_file("rom_config.json", json.dumps(rom_config).encode("utf-8"))

    patch.write_file("token_data.bin", patch.get_token_binary())
