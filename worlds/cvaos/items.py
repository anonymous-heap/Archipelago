from __future__ import annotations

from typing import Dict, List, NamedTuple, TYPE_CHECKING

from BaseClasses import Item, ItemClassification

from .data.pickup_info import rows as pickup_infos
from .data.item_info import item_info_collection
from .item_granting import DISAMBIG_MAX, ID_MAX, TransferCategory, pack
from .options import ForbiddenAreaButton

if TYPE_CHECKING:
    from . import CVAOSWorld


class CVAOSItem(Item):
    """Castlevania: Aria of Sorrow item instance."""

    game: str = "Castlevania - Aria of Sorrow"


class ItemData(NamedTuple):
    classification: ItemClassification
    code: int  # packed AP code (see item_granting.pack / its bit layout)


# Grab ItemInfos by name for classification flags.
_item_info_by_name: Dict[str, object] = {info.name: info for info in item_info_collection}

# Canonical progression: Items that you'd expect to give actual progression.
canonical_progression_names: frozenset[str] = frozenset(
    info.name for info in item_info_collection if info.progression)
# Items that technically _can_ give progression (e.g. breaking a ceiling), but aren't
# realistically going to be in most runs.
logic_only_progression_names: frozenset[str] = frozenset(
    info.name for info in item_info_collection
    if (info.breaks_ceilings or info.breaks_floors) and not info.progression)


def _classification_for_pickup(simple_name: str) -> ItemClassification:
    info = _item_info_by_name.get(simple_name)
    if info:
        if info.progression:
            return ItemClassification.progression
        if info.breaks_ceilings or info.breaks_floors:
            return ItemClassification.progression_skip_balancing
        if info.useful:
            return ItemClassification.useful
        if info.filler:
            return ItemClassification.filler
    return ItemClassification.filler


_MONEY_SUBTYPE = 1


def _transfer_for(pickup) -> tuple[TransferCategory, int]:
    """
    (category, id/value) for a pickup's AP code:
    - For money, (MONEY, gold value)
    - for every other pickup, (PICKUP, item_info.item_number). 
    
    Raises a ValueError if a non-money pickup has no item_info row
    (possibly due to a name mismatch between pickup_info and item_info)
    so the gap fails loudly at load.
    """
    if pickup.subtype_num == _MONEY_SUBTYPE:
        return TransferCategory.MONEY, int(pickup.simple_name)
    info = _item_info_by_name.get(pickup.simple_name)
    if info is None:
        raise ValueError(
            f"pickup {pickup.identifier_key!r} (#{pickup.pickup_number}) has no item_info row for "
            f"name {pickup.simple_name!r}; cannot encode its AP item code")
    return TransferCategory.PICKUP, info.item_number


def _build_item_table() -> Dict[str, ItemData]:
    # TODO(maps): Castle Maps currently encode as plain item transfers (no set_flag). Once the
    # map-reveal EWRAM flag offsets are known, pack them with set_flag=1 so receiving a map
    # reveals it -- item_granting.pack supports it and resolve/grant already apply it.
    copies: Dict[tuple, int] = {}
    table: Dict[str, ItemData] = {}
    for pickup in pickup_infos:
        category, id_or_value = _transfer_for(pickup)
        if id_or_value > ID_MAX:
            raise ValueError(f"{pickup.identifier_key}: id/value {id_or_value} exceeds {ID_MAX} (12 bits)")
        key = (category, id_or_value)
        disambiguation = copies.get(key, 0)
        copies[key] = disambiguation + 1
        if disambiguation > DISAMBIG_MAX:
            raise ValueError(f"more than {DISAMBIG_MAX + 1} copies of {key}; 6-bit disambiguation overflow")
        code = pack(category, id_or_value, disambiguation=disambiguation)
        table[pickup.display_name] = ItemData(_classification_for_pickup(pickup.simple_name), code)
    return table


item_table: Dict[str, ItemData] = _build_item_table()

# --- Synthetic, non-pickup items (shuffled into the pool only under specific options) ---
# Forbidden Area Switch: the shuffled unlock for ForbiddenAreaButton == pickup. It has no ground
# pickup; receiving it sets MISC flag #48 (the Forbidden Area barrier flag), so the barrier opens
# however and wherever the player gets it. In the local world it appears as the Study Sealswitch
# custom pickup (rom/custom_pickups.py), which sets the same flag on collection. misc #48 = bit 16 of
# the u32 at EWRAM (0x344 + (48>>5)*4) = 0x348, i.e. byte 0x34A bit 0.
FORBIDDEN_AREA_SWITCH = "Forbidden Area Switch"
item_table[FORBIDDEN_AREA_SWITCH] = ItemData(
    ItemClassification.progression,
    pack(TransferCategory.FLAG_ONLY, 0, set_flag=True, flag_offset=0x34A, flag_bit=0, flag_value=1),
)
_SYNTHETIC_ITEM_NAMES: frozenset[str] = frozenset({FORBIDDEN_AREA_SWITCH})

# Convenience map for the World class. Kept complete (every pickup, Hard-Mode ones included) so
# item ids stay stable for the data package; per-seed inclusion is decided in create_itempool.
item_name_to_id: Dict[str, int] = {name: data.code for name, data in item_table.items()}

# Items that exist only in the game's Hard Mode (the HARD_PICKUP entities). Excluded from the
# pool unless the Hard Mode option is on; mirrors the location gating in regions.create_regions.
_HARD_PICKUP_ITEM_NAMES: frozenset[str] = frozenset(
    p.display_name for p in pickup_infos if p.is_hard_mode_only)


def create_item(world: "CVAOSWorld", name: str) -> CVAOSItem:
    data = item_table[name]
    return CVAOSItem(name, data.classification, data.code, world.player)


def create_itempool(world: "CVAOSWorld") -> List[CVAOSItem]:
    # One item per pickup. Hard-Mode-only pickups are included only when the Hard Mode option is
    # set, so the pool stays balanced with the locations created in regions.create_regions.
    include_hard = bool(world.options.hard_mode)
    pool = [create_item(world, name) for name in item_table
            if name not in _SYNTHETIC_ITEM_NAMES
            and (include_hard or name not in _HARD_PICKUP_ITEM_NAMES)]
    if world.options.forbidden_area_button.value == ForbiddenAreaButton.option_pickup:
        # Shuffle in the Forbidden Area Switch, displacing one filler so the pool size still equals
        # the location count (the A01 button is removed, not turned into a new check).
        switch = create_item(world, FORBIDDEN_AREA_SWITCH)
        for i, existing in enumerate(pool):
            if existing.classification == ItemClassification.filler:
                pool[i] = switch
                break
        else:
            raise Exception("CVAoS: no filler item available to displace for the Forbidden Area Switch")
    return pool
