"""
The ROM side of the world: building each seed's patch, and the ROM features it applies.

``patch`` builds and applies the patch file. Most other modules build the writes for one
feature, such as ``soul_shuffle`` or ``skull_key_warp``, while ``entity``, ``address_space`` and
``enemy_table`` declare the records and addresses those features share. The names imported
below are the ones the world and its clients use from this package.
"""
from .entity import AoSPickupEntity, ENTITY_SIZE, GBA_ROM_BASE
from .patch import (
    ARCHIPELAGO_IDENTIFIER,
    ARCHIPELAGO_IDENTIFIER_START,
    COMPATIBLE_IDENTIFIERS,
    MIN_SEED_IDENTIFIER,
    RELEASED_IDENTIFIERS,
    AUTH_NUMBER_START,
    CVAOSProcedurePatch,
    get_location_data,
    patch_rom,
)
