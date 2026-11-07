"""
The identifier that each seed's ROM carries, and the identifiers that this version's clients
and installer accept.

The identifier is a 13-character ASCII string, such as ``CVAOS_AP_V0.5``, that the patch writes
into ROM free space. The clients and the collection installer read it to check that they can use
the ROM. Each accepts only the identifiers it knows, so a seed that an older client cannot run
has to carry a newer identifier.

The rules are:

- V0.3 and V0.4 are the released identifiers. Seeds that carry them may still be in progress, so
  this version's clients and installer accept them, along with every newer identifier.
- Every seed that this version generates carries V0.5 or later, so a released client refuses it.
  Its identifier is the newest among those that introduced its ``rom_config.json`` keys
  (ROM_CONFIG_KEY_IDENTIFIERS), or V0.5 when that is newer.
- For a released seed, the client asks the server only for other worlds' items and the starting
  inventory. From V0.5 on, it also asks for the player's own items, because the ROM never hands
  out the item at a defeat location.
"""
from __future__ import annotations

from typing import Dict

# Archipelago metadata written into clean ROM free space. These are *file offsets*, because
# both APProcedurePatch tokens and the BizHawk ROM domain the client reads use file offsets.
# The region at file 0x670000+ (GBA 0x08670000) is well clear of the last real cart data at
# 0x651163 and of the Advance Collection ROM's M2 additions (rom/address_space.py M2_NO_GO:
# 0x660000-0x6610BC and 0x700000-0x7000E3).
ARCHIPELAGO_IDENTIFIER_START = 0x670000   # 13 bytes
# Every identifier, oldest first. Each one is 13 bytes long.
IDENTIFIERS: tuple[str, ...] = ("CVAOS_AP_V0.1", "CVAOS_AP_V0.2", "CVAOS_AP_V0.3",
                                "CVAOS_AP_V0.4", "CVAOS_AP_V0.5", "CVAOS_AP_V0.6")
# The newest identifier this version knows. Clients read len(ARCHIPELAGO_IDENTIFIER) bytes, so
# every identifier has the same length.
ARCHIPELAGO_IDENTIFIER = IDENTIFIERS[-1]
assert all(len(identifier) == len(ARCHIPELAGO_IDENTIFIER) for identifier in IDENTIFIERS)
# The released identifiers. Their seeds have no defeat locations, and their slot data has no
# rom_given_locations, so their clients ask the server only for other worlds' items and the
# starting inventory (client_logic.items_handling_for).
RELEASED_IDENTIFIERS = frozenset({"CVAOS_AP_V0.3", "CVAOS_AP_V0.4"})
# The oldest identifier that a seed from this version can carry. No released client accepts it.
MIN_SEED_IDENTIFIER = "CVAOS_AP_V0.5"
# The identifiers that this version's clients and installer accept: the released ones and every
# newer one. A V0.3 ROM already has its hooks and metadata at 0x670000, and it holds nothing that
# the current client cannot handle.
COMPATIBLE_IDENTIFIERS = frozenset(IDENTIFIERS[IDENTIFIERS.index("CVAOS_AP_V0.3"):])
# The compatible identifiers as a range, oldest first, for messages that name them.
COMPATIBLE_RANGE = (f"{min(COMPATIBLE_IDENTIFIERS, key=IDENTIFIERS.index)} to "
                    f"{max(COMPATIBLE_IDENTIFIERS, key=IDENTIFIERS.index)}")
# The slot's auth, which the client reads to connect.
AUTH_NUMBER_START = 0x670010              # 16 bytes

# Each rom_config.json key, with the identifier of the version that introduced it. A seed carries
# the newest identifier among its keys, and never one older than MIN_SEED_IDENTIFIER
# (identifier_for). apply_rom_features raises on a key that this table lacks, so a patcher that is
# too old for a seed fails before it builds a ROM without that key's feature. A new key needs an
# entry here. It also needs a new identifier when an older client or patcher cannot handle it.
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
    "enemy_drop_rates": "CVAOS_AP_V0.5",
    "drop_table": "CVAOS_AP_V0.6",
}
assert set(ROM_CONFIG_KEY_IDENTIFIERS.values()) <= set(IDENTIFIERS)


def identifier_for(rom_config: Dict[str, object]) -> str:
    """Return the identifier that a seed with ``rom_config`` carries.

    That is the newest identifier among the config's keys, or MIN_SEED_IDENTIFIER when that is
    newer. Raises KeyError for a key that ROM_CONFIG_KEY_IDENTIFIERS lacks.
    """
    identifiers = [MIN_SEED_IDENTIFIER, *(ROM_CONFIG_KEY_IDENTIFIERS[key] for key in rom_config)]
    return max(identifiers, key=IDENTIFIERS.index)
