"""
The item table, item groups and item pool for Castlevania: Aria of Sorrow.
"""

from __future__ import annotations

from collections import Counter
from types import MappingProxyType
from typing import Dict, Iterable, List, Mapping, NamedTuple, Sequence, TYPE_CHECKING

from BaseClasses import Item, ItemClassification

from .data import (EnemyType, enemy_type_collection, item_info_collection,
                   pickup_info_collection as pickup_infos)
from .data.item_balancing import worth
from .enemy_checks import checkable_types, enemy_check_types
from .item_granting import ID_MAX, TransferCategory, pack
from .options import ForbiddenAreaButton, hard_mode_pickups_included

if TYPE_CHECKING:
    from . import CVAOSWorld
    from .drop_shuffle import TravelPlan


class CVAOSItem(Item):
    """Castlevania: Aria of Sorrow item instance."""

    game: str = "Castlevania - Aria of Sorrow"


class ItemData(NamedTuple):
    classification: ItemClassification
    code: int  # packed AP code (see item_granting.pack / its bit layout)


# Each ItemInfo row, by item name.
_item_info_by_name: Dict[str, object] = {info.name: info for info in item_info_collection}

# Items whose only progression use is breaking a ceiling or floor. Logic counts them, but most runs
# never need them for it.
logic_only_progression_names: frozenset[str] = frozenset(
    info.name for info in item_info_collection
    if (info.breaks_ceilings or info.breaks_floors) and not info.progression)


def _classification_for_pickup(simple_name: str) -> ItemClassification:
    info = _item_info_by_name.get(simple_name)
    if info:
        if info.progression:
            return ItemClassification.progression
        if info.breaks_ceilings or info.breaks_floors:
            return ItemClassification.progression_deprioritized_skip_balancing
        if info.useful:
            return ItemClassification.useful
        if info.filler:
            return ItemClassification.filler
    return ItemClassification.filler


_MONEY_SUBTYPE = 1


def _transfer_for(pickup) -> tuple[TransferCategory, int]:
    """
    Return the transfer category and the id or value that a pickup's AP code packs.

    Money gives (MONEY, gold amount), and every other pickup gives (PICKUP, its item_info
    item_number). A non-money pickup with no item_info row raises ValueError, so the world fails to
    load. That usually means the pickup's name in pickup_info differs from the one in item_info.
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
    """Return the items that pickups hold, by name.

    Every copy of an item has the same name and code, as in other worlds, so copies differ only in
    their locations. Location names tell the pickups apart, as in
    "White Dragon Hallway [00D Potion (A)]". Pickups that share an item name but hold different
    items raise ValueError.
    """
    table: Dict[str, ItemData] = {}
    for pickup in pickup_infos:
        category, id_or_value = _transfer_for(pickup)
        if id_or_value > ID_MAX:
            raise ValueError(f"{pickup.identifier_key}: id/value {id_or_value} exceeds {ID_MAX} (12 bits)")
        data = ItemData(_classification_for_pickup(pickup.simple_name), pack(category, id_or_value))
        if table.setdefault(pickup.item_name, data) != data:
            raise ValueError(f"the pickups named {pickup.item_name!r} hold different items")
    return table


item_table: Dict[str, ItemData] = _build_item_table()

# --- Synthetic, non-pickup items (shuffled into the pool only under specific options) ---
# The Study Sealswitch opens the barrier between the Study and the Forbidden Area by setting MISC
# flag #48, the flag the barrier's button sets. Receiving the item from the server sets the flag.
# When the item is at one of this world's pickups, that pickup is the Study Sealswitch custom
# pickup (rom/custom_pickups.py), which sets the same flag when collected. Either way, the player
# sees it named "Study Sealswitch".
FORBIDDEN_AREA_SWITCH = "Study Sealswitch"
item_table[FORBIDDEN_AREA_SWITCH] = ItemData(
    ItemClassification.progression,
    pack(TransferCategory.FLAG_ONLY, 0, set_flag=True, flag_offset=0x34A, flag_bit=0, flag_value=1),
)

# With the Progressive Movement option, three copies of this item replace the Flying Armor,
# Malphas and Giant Bat souls in the pool. The n-th copy a player obtains gives the n-th soul of
# PROGRESSIVE_MOVEMENT_STAGES. Logic (CVAOSWorld.collect) and the client (client_logic) both
# count copies, so the souls always arrive in stage order, whichever location a copy comes from.
PROGRESSIVE_MOVEMENT = "Progressive Movement"
PROGRESSIVE_MOVEMENT_STAGES: tuple[str, ...] = ("Flying Armor", "Malphas", "Giant Bat")
item_table[PROGRESSIVE_MOVEMENT] = ItemData(
    ItemClassification.progression, pack(TransferCategory.PROGRESSIVE, 0))

# --- Enemy check items (enemy_checks.py) ---
# Each defeat location adds one cheap item to the pool, which check_item_names chooses from the
# enemy's common drop. The item has its usual name and code, so an enemy's Potion is the same item
# as a pickup's Potion. A drop that is normally progression counts only as useful here
# (create_enemy_item), so enemy checks never add progression.
# ENEMY_GOLD is an amount that gold pickups hold, so the ROM can show the gold on a pickup too.
ENEMY_GOLD = 100
ENEMY_GOLD_NAME = f"{ENEMY_GOLD} Gold"

# The most copies of one consumable that the game lets Soma carry.
CONSUMABLE_CAP = 9

# enemy name -> the name of its common drop: its slot 1 drop, or else its slot 2 drop. It is ""
# for an enemy that drops nothing.
ENEMY_DROP: Dict[str, str] = {enemy.name: enemy.drops[0] or enemy.drops[1]
                              for enemy in checkable_types()}

# The HP restoratives worth worth.CHEAP_WORTH or less, cheapest first. A check whose enemy's drop
# is worth more adds one of these instead.
HP_RESTORATIVES: tuple[str, ...] = tuple(
    row.name for row in sorted(worth.rows, key=lambda row: (row.worth, row.name))
    if row.item_class == "hp" and row.worth <= worth.CHEAP_WORTH)

# Every item that either drop slot of some enemy holds. Item Drop Shuffle can move any of them into
# the pool.
DROP_ITEMS: frozenset[str] = frozenset(
    item for enemy in enemy_type_collection for item in enemy.drops if item)

# Every item that an enemy check or Item Drop Shuffle can add under some options. Every enemy drop
# is here, so the drops' ids stay the same in the data package whichever items the options pick.
CHECK_ITEM_CHOICES: frozenset[str] = frozenset({ENEMY_GOLD_NAME, *HP_RESTORATIVES, *DROP_ITEMS})
if ENEMY_GOLD_NAME not in item_table:
    raise ValueError(f"no gold pickup holds {ENEMY_GOLD} gold, so the ROM can't show it")
for _name in sorted(CHECK_ITEM_CHOICES):
    if _name not in item_table:
        # An item that no pickup holds is transferred as OTHER_ITEM, the category for an item with
        # no ground pickup.
        _info = _item_info_by_name[_name]
        item_table[_name] = ItemData(_classification_for_pickup(_name),
                                     pack(TransferCategory.OTHER_ITEM, _info.item_number))

# The World's item_name_to_id. It holds every item in item_table whatever the options, including
# the synthetic items, the items that only enemy checks add and Hard Mode pickups' items, so item
# ids stay the same in the data package. create_itempool picks the items that a seed uses.
item_name_to_id: Dict[str, int] = {name: data.code for name, data in item_table.items()}

# Item groups, so that a YAML or a hint can name a kind of item, such as "Gold" or "Blue Souls".
# No red soul is an item, so the "Red Souls" group is never created.
_GROUP_BY_CATEGORY = {"weapon": "Weapons", "armor": "Armor", "accessory": "Accessories",
                      "consumable": "Consumables", "red_soul": "Red Souls", "blue_soul": "Blue Souls",
                      "yellow_soul": "Yellow Souls", "ability_soul": "Ability Souls"}
_SOUL_GROUPS = ("Red Souls", "Blue Souls", "Yellow Souls", "Ability Souls")
item_name_groups: Dict[str, set[str]] = {}
for _name in item_table:
    _info = _item_info_by_name.get(_name)
    if _info is not None:
        _group = _GROUP_BY_CATEGORY[_info.item_category]
        item_name_groups.setdefault(_group, set()).add(_name)
        if _group in _SOUL_GROUPS:
            item_name_groups.setdefault("Souls", set()).add(_name)
    elif _name.endswith(" Gold"):
        item_name_groups.setdefault("Gold", set()).add(_name)


# The item the Study Sealswitch displaces when it is shuffled in: the smallest gold pickup. The
# displaced item leaves the game. Gold pickups differ only in amount, so losing the smallest one
# costs the player the least.
_SEALSWITCH_DISPLACES: str = min(
    (p for p in pickup_infos if p.subtype_num == _MONEY_SUBTYPE and not p.is_hard_mode_only),
    key=lambda p: int(p.simple_name)).item_name


def _cheap_consumable_or_gold(name: str) -> bool:
    row = worth.by_name.get(name)
    return (row is not None and row.category in ("consumable", "money")
            and row.worth <= worth.CHEAP_WORTH)


# Plain filler that every seed can hold: the consumables and gold of the pickups that exist outside
# Hard Mode, each worth no more than worth.CHEAP_WORTH. Core pads item links with this draw, so it
# holds nothing better than what the castle's own pickups offer a quarter of the way in. An item
# appears once for each such pickup, so CVAOSWorld.get_filler_item_name picks an item as often as
# the castle holds it.
FILLER_ITEM_NAMES: tuple[str, ...] = tuple(sorted(
    p.item_name for p in pickup_infos
    if not p.is_hard_mode_only and item_table[p.item_name].classification == ItemClassification.filler
    and _cheap_consumable_or_gold(p.item_name)))


def create_item(world: "CVAOSWorld", name: str) -> CVAOSItem:
    data = item_table[name]
    return CVAOSItem(name, data.classification, data.code, world.player)


def _at_cap(name: str, held: Counter) -> bool:
    info = _item_info_by_name.get(name)
    return info is not None and info.item_category == "consumable" and held[name] >= CONSUMABLE_CAP


def check_item_names(types: Sequence[EnemyType], pool: Iterable[Item],
                     moved_drops: Mapping[str, str] = MappingProxyType({})) -> List[str]:
    """Return the item that each check of ``types`` adds to ``pool``, in the order of ``types``.

    ``moved_drops`` names, by enemy, the drop that Item Drop Shuffle moves to that enemy's check
    (``TravelPlan.check_items``). Such a check adds that drop. These drops count as held before any
    other check's item is chosen, so they are part of every copy count below.

    Any other check adds its enemy's common drop (ENEMY_DROP) when that drop is worth
    worth.CHEAP_WORTH or less. A dearer drop could put mid-game gear early in the run, so its check
    adds an HP restorative instead: the one of HP_RESTORATIVES that the pool holds fewest copies
    of, counting what earlier checks add, and the cheaper one on a tie. A check whose enemy drops
    nothing adds ENEMY_GOLD gold.

    No such check adds a consumable that the pool already holds CONSUMABLE_CAP copies of. A cheap
    drop at the cap gives way to an HP restorative, and an HP restorative at the cap gives way to
    gold.

    The choice depends only on its arguments, which the options decide. So Universal Tracker,
    which rebuilds the pool from the slot data, arrives at the same items.
    """
    held = Counter(item.name for item in pool)
    held.update(moved_drops.values())
    names = []
    for enemy in types:
        name = moved_drops.get(enemy.name)
        if name is None:
            name = _check_item_name(enemy, held)
            held[name] += 1
        names.append(name)
    return names


def _check_item_name(enemy: EnemyType, held: Counter) -> str:
    """Return the item that ``enemy``'s check adds when no moved drop goes there."""
    drop = ENEMY_DROP[enemy.name]
    if not drop:
        return ENEMY_GOLD_NAME
    if worth.item_worth(drop) <= worth.CHEAP_WORTH and not _at_cap(drop, held):
        return drop
    name = min(HP_RESTORATIVES, key=lambda rung: (held[rung], worth.item_worth(rung), rung))
    return ENEMY_GOLD_NAME if _at_cap(name, held) else name


def create_enemy_item(world: "CVAOSWorld", name: str) -> CVAOSItem:
    """Create the item named ``name`` for a defeat location or a moved drop to add to the pool.

    A drop that is normally progression is created as useful, so enemy checks and Item Drop
    Shuffle never add progression. Another copy of the same item, such as one a pickup holds,
    keeps its own classification.
    """
    item = create_item(world, name)
    if item.classification & ItemClassification.progression:
        item.classification = ItemClassification.useful
    return item


def create_itempool(world: "CVAOSWorld") -> List[CVAOSItem]:
    # One item per pickup. Hard-Mode-only pickups are included only when hard_mode_pickups_included
    # returns True, the test regions.create_regions also uses, so item and location counts match.
    include_hard = hard_mode_pickups_included(world.options)
    pool = [create_item(world, pickup.item_name) for pickup in pickup_infos
            if include_hard or not pickup.is_hard_mode_only]
    if world.options.forbidden_area_button.value == ForbiddenAreaButton.option_pickup:
        # Shuffle in the Study Sealswitch, displacing one gold pickup so the pool size still
        # equals the location count. The A01 button is removed rather than turned into a check of
        # its own, so the Sealswitch has no location to add.
        displaced = next(i for i, existing in enumerate(pool)
                         if existing.name == _SEALSWITCH_DISPLACES)
        pool[displaced] = create_item(world, FORBIDDEN_AREA_SWITCH)
    if world.options.progressive_movement.value:
        pool = [create_item(world, PROGRESSIVE_MOVEMENT) if item.name in PROGRESSIVE_MOVEMENT_STAGES
                else item for item in pool]
    # Imported here, because drop_shuffle imports this module through seed_depth and item_smoothing.
    from .drop_shuffle import travel_plan
    travel = travel_plan(world)
    if travel is not None:
        pool = _with_moved_drops(world, pool, travel)
    # One item per defeat location, so the pool still matches the location count.
    moved_to_checks = travel.check_items if travel is not None else MappingProxyType({})
    pool += [create_enemy_item(world, name)
             for name in check_item_names(enemy_check_types(world.options), pool, moved_to_checks)]
    return pool


def _with_moved_drops(world: "CVAOSWorld", pool: List[CVAOSItem],
                      travel: "TravelPlan") -> List[CVAOSItem]:
    """Return ``pool`` with a base restorative swapped out for each moved drop that replaces one.

    The pool size is unchanged. The moved drops that checks add come later, from check_item_names.
    """
    leaving = Counter(travel.base_items_replaced)
    kept = []
    for item in pool:
        if leaving[item.name]:
            leaving[item.name] -= 1
        else:
            kept.append(item)
    if +leaving:
        raise ValueError(f"the pool lacks the base restoratives that drops replace: {+leaving}")
    return kept + [create_enemy_item(world, drop.item)
                   for drop in travel.travelling if drop.base_item is not None]
