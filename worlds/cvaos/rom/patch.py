"""
Build each seed's patch file, and apply it to the base ROM.

Generation runs without the base ROM, so :func:`patch_rom` fills the patch file with plain
writes: the location items from :func:`get_location_data`, the identifier and slot auth, and the
DeathLink hook when that option is on. It also stores ``rom_config.json``, which records the
seed's settings for the features that must read the base ROM. :class:`CVAOSPatchExtension`
applies those features on the player's machine when the patch is applied. The base ROM is a cart
dump or the copy inside the Advance Collection's game.exe (:func:`get_base_rom_bytes`).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Dict, List

from settings import get_settings
from worlds.Files import APPatchExtension, APProcedurePatch, APTokenMixin, APTokenTypes

from .. import item_granting
from ..data import item_info_collection, pickup_info_collection as pickup_infos
from ..constants import AC_USA_ROM_MD5, USA_ROM_MD5
from ..items import ENEMY_ITEM, FORBIDDEN_AREA_SWITCH, item_table
from . import (classicvania_movement, custom_pickups, deathlink_hook, forbidden_area_button,
               inventory_menu, oops_all_whips, received_item_box, single_jump_divekick,
               skull_key_pickup, skull_key_warp, soul_drop_rates, soul_guarantee_hook,
               soul_shuffle)
from .entity import GBA_ROM_BASE, PICKUP, AoSPickupEntity
from .._bytemaker_compat import offset_of
from ..options import (ForbiddenAreaButton, SoulShuffle, TARGET_ADVANCE_COLLECTION,
                       TARGET_GBA)

if TYPE_CHECKING:
    from BaseClasses import Location
    from .. import CVAOSWorld

# Archipelago metadata written into clean ROM free space. These are *file offsets*, because
# both APProcedurePatch tokens and the BizHawk ROM domain the client reads use file offsets.
# The region at file 0x670000+ (GBA 0x08670000) is well clear of the last real cart data at
# 0x651163 and of the Advance Collection ROM's M2 additions (rom/address_space.py M2_NO_GO:
# 0x660000-0x6610BC and 0x700000-0x7000E3).
# The identifier is also the client/patch compatibility gate. A client and the collection
# installer accept only the identifiers they know, so a seed that an older client can't run must
# carry a newer identifier.
ARCHIPELAGO_IDENTIFIER_START = 0x670000   # 13 bytes
# Every identifier, oldest first. Each one is 13 bytes long.
IDENTIFIERS: tuple[str, ...] = ("CVAOS_AP_V0.1", "CVAOS_AP_V0.2", "CVAOS_AP_V0.3",
                                "CVAOS_AP_V0.4", "CVAOS_AP_V0.5")
# The newest identifier this version knows. Clients read len(ARCHIPELAGO_IDENTIFIER) bytes.
ARCHIPELAGO_IDENTIFIER = IDENTIFIERS[-1]
assert all(len(identifier) == len(ARCHIPELAGO_IDENTIFIER) for identifier in IDENTIFIERS)
# The released identifiers. Their seeds have no defeat locations and no rom_given_locations in
# their slot data, so their clients ask the server only for other worlds' items and the starting
# inventory (client_logic.items_handling_for).
RELEASED_IDENTIFIERS = frozenset({"CVAOS_AP_V0.3", "CVAOS_AP_V0.4"})
# Every seed from this version carries at least this identifier, so a released client refuses it.
# Its client asks the server for the player's own items too, because the ROM never hands out the
# item at a defeat location.
MIN_SEED_IDENTIFIER = "CVAOS_AP_V0.5"
# Identifiers this version's clients and installer accept: the released ones, whose seeds may
# still be in progress, and every newer one. A V0.3 ROM already has its hooks and metadata at
# 0x670000 and holds nothing the current client cannot handle.
COMPATIBLE_IDENTIFIERS = frozenset(IDENTIFIERS[IDENTIFIERS.index("CVAOS_AP_V0.3"):])
AUTH_NUMBER_START = 0x670010              # 16 bytes

# Each rom_config.json key, with the identifier of the version that introduced it. A seed carries
# the newest identifier among its keys, and never one older than MIN_SEED_IDENTIFIER
# (identifier_for). apply_rom_features raises on a key that this table lacks, so a patcher that is
# too old for a seed fails before it builds a ROM without that key's feature. A new key needs an
# entry here, and a new identifier when an older client or patcher cannot handle it.
ROM_CONFIG_KEY_IDENTIFIERS: dict[str, str] = {
    "skull_key_warp": "CVAOS_AP_V0.2",
    "forbidden_area_pickup": "CVAOS_AP_V0.2",
    "single_jump_divekick": "CVAOS_AP_V0.2",
    "classicvania_movement": "CVAOS_AP_V0.2",
    "oops_all_whips": "CVAOS_AP_V0.2",
    "soul_shuffle_plan": "CVAOS_AP_V0.2",
    "keep_soul_drop_rates": "CVAOS_AP_V0.2",
    "shuffle_starting_soul": "CVAOS_AP_V0.2",
    "multiply_soul_drop_rates": "CVAOS_AP_V0.2",
    "soul_drop_rate_multiplier": "CVAOS_AP_V0.2",
    "target_platform": "CVAOS_AP_V0.3",
    "ancient_book_soul_drops": "CVAOS_AP_V0.3",
}
assert set(ROM_CONFIG_KEY_IDENTIFIERS.values()) <= set(IDENTIFIERS)


def identifier_for(rom_config: Dict[str, object]) -> str:
    """Return the identifier that a seed with ``rom_config`` carries.

    It is the newest identifier among the config's keys, or MIN_SEED_IDENTIFIER when that is
    newer. Raises KeyError for a key that ROM_CONFIG_KEY_IDENTIFIERS lacks.
    """
    identifiers = [MIN_SEED_IDENTIFIER, *(ROM_CONFIG_KEY_IDENTIFIERS[key] for key in rom_config)]
    return max(identifiers, key=IDENTIFIERS.index)


# SoulShuffle option value -> rom/soul_shuffle mode.
_SOUL_SHUFFLE_MODES = {
    SoulShuffle.option_off: soul_shuffle.MODE_OFF,
    SoulShuffle.option_within_type: soul_shuffle.MODE_WITHIN_TYPE,
    SoulShuffle.option_any_type: soul_shuffle.MODE_ANY_TYPE,
}


# Item encoding lookup: AP item *code* -> (type_num, subtype_num, item_offset). Keyed by the stable
# packed code (not the name) so renaming AP items can never desync ROM item placement.


def _item_encodings() -> Dict[int, tuple[int, int, int]]:
    """Return the encoding of each item that the ROM can show on a pickup, by its code.

    An item that a vanilla pickup holds uses that pickup's subtype and item_offset. Its type is
    always PICKUP, even for the five items that vanilla holds only in HARD_PICKUPs, which the game
    spawns only in Hard Mode. One code stands for every copy of an item, and a copy placed anywhere
    else, by a plando or an item link for example, has to spawn in normal mode too.

    PICKUP is also correct for the copies that those five HARD_PICKUPs add to the pool. They are
    in the pool only when Hard Mode is on or Hard Mode Pickups is ``always``. With Hard Mode on,
    which the client enforces, a PICKUP spawns just as a HARD_PICKUP would. With ``always``, the
    items are meant to spawn in normal mode.

    An item that only an enemy check adds follows its category's rule: every vanilla pickup of a
    category stores the same subtype, and an item_offset that is the item's id plus a fixed
    amount. Raises ValueError when the vanilla pickups disagree on an item's encoding or on their
    category's rule.
    """
    info_by_name = {info.name: info for info in item_info_collection}
    info_by_number = {info.item_number: info for info in item_info_collection}
    encodings: Dict[int, tuple[int, int, int]] = {}
    rules: Dict[str, tuple[int, int, int]] = {}
    for p in pickup_infos:
        encoding = (PICKUP, p.subtype_num, p.item_offset)
        if encodings.setdefault(item_table[p.item_name].code, encoding) != encoding:
            raise ValueError(f"the pickups holding {p.item_name} disagree on their encoding")
        info = info_by_name.get(p.simple_name)
        if info is None:
            continue  # gold
        rule = (PICKUP, p.subtype_num, p.item_offset - info.id)
        if rules.setdefault(info.item_category, rule) != rule:
            raise ValueError(
                f"{p.display_name}: {info.item_category} pickups disagree on their encoding")
    for name in ENEMY_ITEM.values():
        code = item_table[name].code
        if code not in encodings:
            info = info_by_number[item_granting.resolve(code).id_or_value]
            type_num, subtype_num, added = rules[info.item_category]
            encodings[code] = (type_num, subtype_num, info.id + added)
    return encodings


_item_encoding: Dict[int, tuple[int, int, int]] = _item_encodings()


def get_item_encoding(item_code: int) -> tuple[int, int, int]:
    """Return (type_num, subtype_num, item_offset) for a CVAoS item by its packed AP code."""
    return _item_encoding[item_code]


# Placeholder for a location whose item the ROM does not give (see rom_gives_item), such as another
# world's item. The vanilla collect path always grants an item, and the item data has no "null
# item", so the placeholder is a Skull Key (PICKUP type 4, consumable subtype 2, item_offset 25).
# No vanilla pickup holds a Skull Key, so one on the ground stands out as an AP placeholder.
# The location check does not depend on what the pickup grants, because the client reads the
# collected-pickup save flags (PICKUP_FLAGS). The type stays 4 so that the entity still sets its
# save flag. Granting nothing and showing a "sent X to Player Y" textbox instead would need the
# collect path to recognise the placeholder, and the collect hook in rom/custom_pickups.py
# handles only custom pickups.
_AP_PLACEHOLDER = (4, 2, 25)  # Skull Key

# Synthetic items (no ground pickup) that appear in the local world as a custom behaviour-pickup.
# The Study Sealswitch AP item spawns the in-game Study Sealswitch custom pickup, which sets misc
# #48 on collection. Its FLAG_ONLY receive path sets the same flag, so the unlock works whether the
# item is collected locally or received from elsewhere. Keyed by item name (codes are packed, not
# name-derived).
_ITEM_CUSTOM_PICKUP = {FORBIDDEN_AREA_SWITCH: custom_pickups.STUDY_SEALSWITCH}

# Where the two edited fields sit inside the 12-byte entity record, from the record's own
# layout rather than counted by hand. ``type`` and ``subtype`` are adjacent, so one write
# covers both.
_TYPE_OFF = offset_of(AoSPickupEntity, "type")
_ITEM_OFF = offset_of(AoSPickupEntity, "var_b")   # var_b carries the item_offset
assert offset_of(AoSPickupEntity, "subtype") == _TYPE_OFF + 1

# Location writes: ROM file offset -> bytes

def rom_gives_item(world, location) -> bool:
    """Whether the ROM hands out ``location``'s item itself when its pickup is collected.

    It does for this player's own items, except a Progressive Movement copy, which has no pickup
    of its own. Every other player's item, including an item-link item that this player's own
    pickup holds for its link group, appears as the placeholder, and the server delivers it.
    """
    item = location.item
    return item.player == world.player and (item.code in _item_encoding
                                            or item.name in _ITEM_CUSTOM_PICKUP)


def get_location_data(world: CVAOSWorld, active_locations: List[Location]) -> Dict[int, bytes]:
    """Build a dict of {rom_file_offset: bytes_to_write} for every location.

    Each location's entity takes its subtype and item_offset from the item placed there. Its type
    is always PICKUP (see _item_encodings), so it spawns in normal mode and in Hard Mode alike.
    """
    writes: Dict[int, bytes] = {}

    for loc in active_locations:
        rom_offset = loc.address - GBA_ROM_BASE

        custom = custom_pickups.CUSTOM_PICKUP_TEST_PLACEMENTS.get(loc.name)
        if custom is None and loc.item.player == world.player:
            # This player's synthetic items, such as the Study Sealswitch, spawn their in-game
            # custom pickup. Another player's copy appears as the placeholder, and that player
            # receives it through its FLAG_ONLY receive path.
            custom = _ITEM_CUSTOM_PICKUP.get(loc.item.name)
        if custom is not None:
            type_num, subtype_num, item_offset = custom_pickups.get_encoding(custom)
        elif rom_gives_item(world, loc):
            type_num, subtype_num, item_offset = get_item_encoding(loc.item.code)
        else:
            # Another world's item uses the placeholder. So does a Progressive Movement copy,
            # because it has no pickup of its own. When its location is checked, the client
            # grants the next soul in PROGRESSIVE_MOVEMENT_STAGES.
            type_num, subtype_num, item_offset = _AP_PLACEHOLDER

        writes[rom_offset + _TYPE_OFF] = bytes([type_num, subtype_num])
        writes[rom_offset + _ITEM_OFF] = item_offset.to_bytes(2, "little")

    return writes


# Base ROM and patch classes

def _read_base_rom_source(path: str) -> bytes:
    """Read a base ROM from *path*, which may be a GBA ROM or the collection's game.exe.

    A path ending in ``.exe``, or a file that starts with the PE magic ``MZ``, is treated as the
    collection's game.exe. The stock AoS ROM is then extracted from the ``windata/`` archive next
    to it, so the exe must be the real install file, not a copy. Any other file is returned as-is.
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


# Set by base_rom_override while the collection installer applies a patch.
_base_rom_override: bytes | None = None


@contextlib.contextmanager
def base_rom_override(base: bytes):
    """Patch from ``base`` instead of the configured source while the block runs.

    The collection installer uses this so that every patch it applies starts from the ROM of the
    collection it installs into. A gba-target patch applies to that ROM as well as to a cart dump.
    """
    global _base_rom_override
    previous, _base_rom_override = _base_rom_override, base
    try:
        yield
    finally:
        _base_rom_override = previous


def get_base_rom_bytes(target: int = TARGET_GBA) -> bytes:
    """The base ROM to patch against, chosen by the seed's declared target platform.

    - ``advance_collection``: source the ROM from the installed collection automatically. The
      stock AoS ROM is pulled out of ``windata/`` next to the collection's game.exe, which is
      ``collection_exe`` or else the one in the player's Steam libraries (find_collection_exe),
      so a collection player configures nothing. Only if the collection can't be found does it
      fall back to the prompted ``rom_file``.
    - ``gba``: use ``rom_file``; accessing it opens a file browser when unset. The pick may be a
      GBA ROM *or* the collection's game.exe (``_read_base_rom_source`` extracts from the exe).

    While :func:`base_rom_override` is active, its ROM is returned instead, whatever the target.
    """
    if _base_rom_override is not None:
        return _base_rom_override
    opts = get_settings().cvaos_options
    if target == TARGET_ADVANCE_COLLECTION:
        from ..advance_collection.install import extract_base_rom_bytes, find_collection_exe
        configured = str(opts.collection_exe)
        exe = find_collection_exe(opts)
        if exe is not None:
            if exe != configured:
                get_settings().save()  # found in a Steam library, so remember it
            return extract_base_rom_bytes(exe)
    return _read_base_rom_source(str(opts.rom_file))


class CVAOSPatchExtension(APPatchExtension):
    game = "Castlevania - Aria of Sorrow"

    @staticmethod
    def apply_rom_features(caller: APProcedurePatch, rom: bytes, config_file_name: str) -> bytes:
        """
        Apply the features that must read the base ROM, on the player's machine.

        ``config_file_name`` names the JSON file in the patch that records which features the seed
        enabled. Most features read the ROM as patched so far, so the order below matters.

        Raises ValueError when the config holds a key that this version does not know, because
        the seed then needs a feature that this version can't apply.
        """

        config = json.loads(caller.get_file(config_file_name).decode("utf-8"))
        _check_known_keys(config, rom)
        working = bytearray(rom)

        def apply(writes: Dict[int, bytes]) -> None:
            for offset, data in writes.items():
                working[offset:offset + len(data)] = data

        # Skull Key -> warp consumable hook (rom/skull_key_warp.py).
        if config["skull_key_warp"]:
            apply(skull_key_warp.build_writes())

        # Ancient Book soul drops: the enemy-death roll hook (rom/soul_guarantee_hook.py). It checks
        # the instructions it replaces against the base ROM, which is why it is applied here. The
        # book descriptions are installed by repointing entries in the string-pointer array, so
        # they must be applied before the Item-Use menu extension further down copies that array.
        if config.get("ancient_book_soul_drops"):
            apply(soul_guarantee_hook.build_writes(bytes(working)))
            apply(soul_guarantee_hook.build_description_writes(bytes(working)))

        # Forbidden Area button (rom/forbidden_area_button.py): with the pickup option, the A01
        # press-button becomes an inert object, so only the shuffled Study Sealswitch opens the
        # barrier.
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
        # and pickup SFX for an item or soul another world sent. It is always applied, because
        # announcing what you receive is not a gameplay option. The writes do nothing until the
        # client posts a request to the mailbox. Its place in this order does not matter,
        # since it registers a slot in the shared update-hook framework (rom/xanthus_framework.py),
        # which is idempotent.
        apply(received_item_box.build_writes(bytes(working)))

        # Skull Key pickups at the cap (rom/skull_key_pickup.py): the game refuses a consumable
        # pickup while the player holds 9, and this patch exempts the Skull Key. It is always
        # applied, because every other world's item appears as a Skull Key and has to stay
        # collectable at any count.
        apply(skull_key_pickup.build_writes())

        # Custom-pickup framework (rom/custom_pickups.py): collect hook, extended consumable-icon
        # table, and the custom icons' tiles.
        apply(custom_pickups.build_writes_from_rom(bytes(working)))

        # Item-Use menu extension (rom/inventory_menu.py): relocated name/desc/string tables built
        # from the current rom state, so all repoints above are included.
        apply(inventory_menu.build_writes(bytes(working)))

        return bytes(working)

    @staticmethod
    def apply_enemy_table(caller: APProcedurePatch, rom: bytes, config_file_name: str) -> bytes:
        """
        Apply the enemy-table edits that must come after every other feature.

        A rate edit that scales the rates every earlier feature left, such as Enemy Drop Rate's,
        belongs here. No ``rom_config.json`` key uses this step yet, so it returns ``rom``
        unchanged.

        Every seed from V0.5 on lists this step, while a released patch lists only the two before
        it. An apworld from before V0.5 therefore refuses a newer patch with "Unknown procedure"
        instead of building a ROM without these edits.
        """
        return rom


def _check_known_keys(config: Dict[str, object], rom: bytes) -> None:
    """Raise ValueError when ``config`` holds a key that ROM_CONFIG_KEY_IDENTIFIERS lacks.

    The message names the identifier that the patch's tokens wrote into ``rom``, when there is
    one, because that is the version the seed needs.
    """
    unknown = sorted(set(config) - set(ROM_CONFIG_KEY_IDENTIFIERS))
    if not unknown:
        return
    start = ARCHIPELAGO_IDENTIFIER_START
    identifier = rom[start:start + len(ARCHIPELAGO_IDENTIFIER)].decode("ascii", "replace")
    needed = (f" The seed carries {identifier}, so it needs an apworld of that version or later."
              if identifier.startswith("CVAOS_AP_V") else "")
    raise ValueError(
        f"This Aria of Sorrow patch needs a newer apworld. Its rom_config.json has "
        f"{', '.join(unknown)}, which this apworld can't apply.{needed} Install the apworld that "
        f"generated the seed, then apply the patch again.")


class CVAOSProcedurePatch(APProcedurePatch, APTokenMixin):
    # Two accepted bases on purpose: the cart dump and the collection's own copy of the ROM,
    # which M2 altered (constants.py). The patch bytes are identical against either.
    hash = [USA_ROM_MD5, AC_USA_ROM_MD5]
    patch_file_ending: str = ".apcvaos"
    result_file_ending: str = ".gba"
    game = "Castlevania - Aria of Sorrow"

    # A seed's manifest stores this list, and patching follows the manifest. A released patch
    # therefore still applies without the last step.
    procedure = [
        ("apply_tokens", ["token_data.bin"]),
        ("apply_rom_features", ["rom_config.json"]),
        ("apply_enemy_table", ["rom_config.json"]),
    ]

    @classmethod
    def get_source_data(cls) -> bytes:
        return cls._checked_base(get_base_rom_bytes())

    @classmethod
    def _checked_base(cls, base: bytes) -> bytes:
        """Return ``base`` if it is one of the two accepted base ROMs, and raise otherwise.

        AP records ``hash`` in the patch but never compares it. Without this check, a wrong base
        (a previous seed's .gba, say) would fail only later, at some feature's ROM-mismatch guard.
        """
        digest = hashlib.md5(base).hexdigest()
        if digest not in cls.hash:
            raise ValueError(
                f"The base ROM is not an unmodified Castlevania: Aria of Sorrow (USA) ROM (MD5 "
                f"{digest}). Point the patcher at the original US ROM, or at the Advance "
                f"Collection's game.exe, not at a ROM that was already patched.")
        return base

    def get_source_data_with_cache(self) -> bytes:  # type: ignore[override]
        # This replaces the base class's cached classmethod, because the base ROM's source depends
        # on the seed. patch_rom stores the YAML's target platform in rom_config.json, and patch()
        # has already loaded the container's files when this runs. An advance_collection seed
        # therefore reads the collection's ROM, while a gba seed asks for a ROM or game.exe.
        # Nothing is cached, since each patch is applied once.
        try:
            target = int(json.loads(self.get_file("rom_config.json")).get("target_platform",
                                                                          TARGET_GBA))
        except (KeyError, ValueError, json.JSONDecodeError):
            target = TARGET_GBA
        return check_base_for_target(self._checked_base(get_base_rom_bytes(target)), target)


def check_base_for_target(base: bytes, target: int) -> bytes:
    """Return ``base``, warning when a seed made for the collection is patched from a cart dump.

    An advance_collection seed falls back to ``rom_file`` when no collection is found. If that file
    is a cart dump, the patched ROM plays in an emulator but not in the collection. The
    collection's installer already refuses such a ROM, so this only warns.
    """
    if target == TARGET_ADVANCE_COLLECTION and hashlib.md5(base).hexdigest() == USA_ROM_MD5:
        logging.getLogger("Patch").warning(
            "This seed was generated for the Advance Collection but is being patched from a cart "
            "dump. The result plays in BizHawk. To play it in the collection, install the .apcvaos "
            "file with the Launcher's \"CVAoS Collection ROM Installer\" instead.")
    return base


def patch_rom(world: CVAOSWorld, patch: CVAOSProcedurePatch, offset_data: Dict[int, bytes]) -> None:
    """Fill the patch container without reading the base ROM.

    Everything that can be derived without the base ROM is written as a token.
    ``rom_config.json`` records which ROM-reading features this seed enabled, for
    ``apply_rom_features`` to apply on the player's machine.
    """
    rom_config = build_rom_config(world)

    for offset, data in offset_data.items():
        patch.write_token(APTokenTypes.WRITE, offset, data)

    # AP metadata in ROM free space: the identifier the client validates, and the
    # slot auth it reads to connect. The config's keys decide the identifier.
    patch.write_token(APTokenTypes.WRITE, ARCHIPELAGO_IDENTIFIER_START,
                      identifier_for(rom_config).encode("ascii"))
    patch.write_token(APTokenTypes.WRITE, AUTH_NUMBER_START, bytes(world.auth))

    # DeathLink "real kill" hook (the "Death Link" option; see rom/deathlink_hook.py). It is a
    # fixed blob and veneer that needs no ROM reads, so it can be written as plain tokens.
    if world.options.death_link:
        for offset, data in deathlink_hook.build_writes().items():
            patch.write_token(APTokenTypes.WRITE, offset, data)

    patch.write_file("rom_config.json", json.dumps(rom_config).encode("utf-8"))

    patch.write_file("token_data.bin", patch.get_token_binary())


def build_rom_config(world: CVAOSWorld) -> Dict[str, object]:
    """Return the seed's ``rom_config.json``: the settings of the features that read the base ROM.

    Everything that must READ the base ROM (guards, relocated tables, icon extraction) is
    deferred to the patch extension's steps through this config. Its keys must match what those
    steps look up, and each key needs an entry in ROM_CONFIG_KEY_IDENTIFIERS. target_platform
    also drives base-ROM source selection at apply time (get_source_data_with_cache), and
    apply_rom_features ignores it.
    """
    return {
        "target_platform": world.options.target_platform.value,
        "skull_key_warp": bool(world.options.skull_key_warp),
        "ancient_book_soul_drops": bool(world.options.ancient_book_soul_drops),
        "forbidden_area_pickup":
            world.options.forbidden_area_button.value == ForbiddenAreaButton.option_pickup,
        "single_jump_divekick": bool(world.options.single_jump_divekick),
        "classicvania_movement": bool(world.options.classicvania_movement),
        "oops_all_whips": bool(world.options.oops_all_whips),
        # Which enemy now drops which enemy's vanilla soul. soul_shuffle carries the vanilla
        # table, so the plan is drawn here from the seed, without the ROM, and applied against
        # the base ROM later.
        "soul_shuffle_plan": soul_shuffle.plan_shuffle(
            world.random, _SOUL_SHUFFLE_MODES[world.options.soul_shuffle.value]),
        "keep_soul_drop_rates": bool(world.options.keep_soul_drop_rates),
        "shuffle_starting_soul": bool(world.options.shuffle_starting_soul),
        "multiply_soul_drop_rates": bool(world.options.multiply_soul_drop_rates),
        "soul_drop_rate_multiplier": int(world.options.soul_drop_rate_multiplier.value),
    }
