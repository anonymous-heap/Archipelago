"""
Item Drop Shuffle: which enemy drops move into the item pool, what each enemy drops instead, and
how the drops are then dealt across the castle.

Each enemy type's row of the stat table has two drop slots. Slot 1 is the common drop and slot 2
the rare one, and 96 of the 226 slots hold an item. A drop that moves into the pool is a
travelling drop, and the slot it leaves takes a similar item from this game instead.

Worth comes from ``worth.csv`` throughout, and the cheapest item is the one with the lowest worth.
Equipment means weapons, armor and accessories, and every other item is a consumable.

The shuffle works in two steps, because the item pool is built long before the seed's depths are
known:

1. :func:`plan_travel` returns the travel plan: which drops travel, which filler item each one
   replaces in the pool, and which item fills each slot that a travelling drop vacated. The plan
   depends only on options, so Universal Tracker can rebuild the pool from the slot data.
2. :func:`plan_drop_shuffle` then deals the slots' contents by each enemy's seed depth. Within
   each slot kind (common or rare) and item kind (equipment or consumable), the cheapest contents
   go to the earliest and weakest enemies, and the best to the last and toughest.

For one world, :func:`travel_plan` returns the travel plan for its options, and
:func:`final_drop_table` returns the table that the patch writes. That table starts as the dealt
one when Item Drop Shuffle is on, and as the game's own (:func:`vanilla_drop_table`) otherwise.
With Wide Item Smoothing on, ``wide_smoothing.band_drop_table`` then moves each drop worth more
than its enemy's seed depth plus ``wide_smoothing.BAND``. ``drop_sources`` prints the final table
for the spoiler and the /drops command.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType, SimpleNamespace
from typing import TYPE_CHECKING, Dict, List, Mapping, Optional, Tuple

from .data import enemy_type_collection
from .data.item_balancing import worth
from .enemy_checks import enemy_check_types
from .options import EnemyChecks, Goal
from .seed_depth import UNREACHED, enemy_seed_depths

if TYPE_CHECKING:
    from . import CVAOSWorld

__all__ = [
    "BASE_RESTORATIVES",
    "EXCLUDED_FROM_SUPPLY",
    "HAMMER_STOCK",
    "HP_WEIGHT",
    "REPLACEMENT_WINDOW",
    "DropShufflePlan",
    "DropSlot",
    "DropTable",
    "SlotKey",
    "TravelPlan",
    "TravellingDrop",
    "deal",
    "final_drop_table",
    "plan_drop_shuffle",
    "plan_travel",
    "travel_plan",
    "vanilla_drop_table",
]

SlotKey = Tuple[int, int]  # (enemy id, slot), with slot 1 the common drop and 2 the rare one

EQUIPMENT_CATEGORIES = ("weapon", "armor", "accessory")
RESTORATIVE_AND_CURE_CLASSES = ("hp", "mp", "cure")

# A vacated slot takes an item whose worth is within this much of the travelling drop's, when the
# supply has one that close.
REPLACEMENT_WINDOW = 0.05
# An enemy's level, which orders the deal, is its seed depth plus this weight times its HP rank.
HP_WEIGHT = 0.1

# The castle's own restoratives that travelling drops replace in the pool once the enemy checks'
# filler runs out. Each entry gives a restorative and how many of its copies may be replaced. They
# are used in this order, cheapest first, and every option combination's pool holds at least these
# copies.
BASE_RESTORATIVES: Tuple[Tuple[str, int], ...] = (
    ("Potion", 4), ("Mind Up", 3), ("High Potion", 5), ("High Mind Up", 4))

# The weapons, armor and accessories that Hammer's shop sells, from its three stock lists (GBA
# 0x08526C04, 0x08526C1C and 0x08526C40). A player can buy these whether or not they drop, so
# they stay in the supply even when they travel.
HAMMER_STOCK = frozenset({
    "Baselard", "Combat Knife", "Rapier", "Hammer", "Broadsword", "Spear", "Gladius", "Scimitar",
    "Katana", "Cutall", "Warhammer", "Estoc", "Trident", "Lance", "Claymore", "Partizan", "Gram",
    "Ascalon", "Leather Plate", "War Fatigues", "Copper Plate", "Soldier Uniform", "Iron Plate",
    "Army Jacket", "Steel Plate", "Armor of Water", "Silver Plate", "Gold Plate", "Crimson Cloak",
    "Soul Eater Ring"})

# Items that never fill a vacated slot. Valmanway, Chaos Ring and Soul Eater Ring stay out of
# every drop slot. worth.csv rates them 1.0, so no replacement comes near them, and the supply
# leaves them out as well. No pickup or drop in the game holds the other four, so how they behave
# as a spawned drop is untested.
EXCLUDED_FROM_SUPPLY = frozenset({
    "Valmanway", "Chaos Ring", "Soul Eater Ring",
    "Positron Rifle", "Ascalon", "Excalibur", "Silver Plate"})


@dataclass(frozen=True)
class DropSlot:
    """The contents of one drop slot: an item name, or None when the slot is empty, and a rarity
    byte.

    The rarity byte stays with its item when the deal moves it. A vanilla drop carries the byte of
    its vanilla slot, and a replacement carries the byte of the slot it first filled. Enemy Drop
    Rate may scale the byte later, when the patch is applied.
    """

    item: Optional[str]
    rarity: int


@dataclass(frozen=True)
class TravellingDrop:
    """A vanilla drop that leaves its slot for the item pool.

    ``item`` leaves slot ``slot`` of enemy ``enemy_id``, and ``replacement`` is the item that fills
    the vacated slot.

    In the pool, the drop takes the place of one filler item. Either the defeat location of the
    enemy named ``check`` adds it instead of that location's own filler, or it replaces one copy
    of ``base_item``, a restorative from the castle's own pickups. Exactly one of ``check`` and
    ``base_item`` is set.
    """

    enemy_id: int
    slot: int
    item: str
    replacement: str
    check: Optional[str] = None
    base_item: Optional[str] = None


@dataclass(frozen=True)
class TravelPlan:
    """What Item Drop Shuffle moves under one set of options, before any seed depth is known."""

    travelling: Tuple[TravellingDrop, ...]  # cheapest first
    supply: Tuple[str, ...]                 # the items a vacated slot may take, cheapest first
    contents: Mapping[SlotKey, DropSlot]    # each filled slot, with replacements in place

    @property
    def check_items(self) -> Mapping[str, str]:
        """The travelling drop that each enemy's defeat location adds, by enemy name."""
        return MappingProxyType({drop.check: drop.item for drop in self.travelling if drop.check})

    @property
    def base_items_replaced(self) -> Counter:
        """How many copies of each base restorative leave the pool for a travelling drop."""
        return Counter(drop.base_item for drop in self.travelling if drop.base_item)


@dataclass(frozen=True)
class DropShufflePlan:
    """One seed's Item Drop Shuffle: the travel plan, which depends only on options, and the drop
    table dealt by seed depth."""

    travel: TravelPlan
    table: Mapping[int, Tuple[DropSlot, DropSlot]]  # each enemy id's two slots, common first


def _item_worth(name: str) -> float:
    return worth.item_worth(name)


def _is_equipment(name: str) -> bool:
    return worth.by_name[name].category in EQUIPMENT_CATEGORIES


# Every filled slot of the vanilla table, by (enemy id, slot).
_VANILLA_SLOTS: Mapping[SlotKey, DropSlot] = MappingProxyType({
    (enemy.id, slot): DropSlot(item, enemy.drop_rarities[slot - 1])
    for enemy in enemy_type_collection
    for slot, item in enumerate(enemy.drops, start=1) if item})


def vanilla_drop_table() -> Mapping[int, Tuple[DropSlot, DropSlot]]:
    """Return the game's own drop table, in the form :attr:`DropShufflePlan.table` uses."""
    return MappingProxyType({
        enemy.id: tuple(DropSlot(item or None, rarity)
                        for item, rarity in zip(enemy.drops, enemy.drop_rarities))
        for enemy in enemy_type_collection})


def _checked_enemies(enemy_checks: int, goal: int) -> Tuple[str, ...]:
    # enemy_check_types reads only these two options, so they stand in for the full set.
    options = SimpleNamespace(enemy_checks=EnemyChecks(enemy_checks), goal=Goal(goal))
    return tuple(enemy.name for enemy in enemy_check_types(options))


def _supply(travelling_items: frozenset) -> Tuple[str, ...]:
    """Return the items a vacated slot may take, cheapest first.

    The supply holds every restorative and cure, Hammer's stock, and every weapon, armor and
    accessory not in ``travelling_items``, including those the castle also holds as pickups.
    EXCLUDED_FROM_SUPPLY is left out.
    """
    items = set(HAMMER_STOCK)
    for row in worth.rows:
        if row.category == "consumable" and row.item_class in RESTORATIVE_AND_CURE_CLASSES:
            items.add(row.name)
        elif row.category in EQUIPMENT_CATEGORIES and row.name not in travelling_items:
            items.add(row.name)
    return tuple(sorted(items - EXCLUDED_FROM_SUPPLY, key=lambda name: (_item_worth(name), name)))


def _replacement(left: str, supply: Tuple[str, ...], used: Counter) -> str:
    """Return the supply item that fills the slot that the travelling drop ``left`` vacated.

    The candidates are the supply items within REPLACEMENT_WINDOW of the drop's worth. Among them,
    an item of the same kind (equipment or consumable) comes first, then the one used least so
    far, then the one closest in worth. When no supply item is that close, the closest in worth
    is chosen.
    """
    target = _item_worth(left)
    candidates = [name for name in supply if name != left]
    near = [name for name in candidates
            if abs(_item_worth(name) - target) <= REPLACEMENT_WINDOW + 1e-9]
    if not near:
        return min(candidates, key=lambda name: (abs(_item_worth(name) - target), name))
    equipment = _is_equipment(left)
    return min(near, key=lambda name: (_is_equipment(name) != equipment, used[name],
                                       abs(_item_worth(name) - target), name))


@lru_cache(maxsize=None)
def plan_travel(enemy_checks: int, goal: int, wide: bool) -> TravelPlan:
    """Return the travel plan for the given Enemy Checks and Goal option values.

    ``wide`` says whether Wide Item Smoothing is on. The result depends on nothing else.

    * **Which drops travel.** A drop is a candidate when its worth is below
      ``worth.STRONG_WORTH``. Unless ``wide`` is on, it must also be worth no more than
      ``worth.CHEAP_WORTH``. The cheapest candidates travel, as many as there are places for
      them: one per enemy check, plus one per copy in BASE_RESTORATIVES.
    * **Places.** An enemy check adds its own enemy's travelling drop, from slot 1 when both
      slots travel. The other travelling drops, cheapest first, take the places of other checks'
      filler, starting with the checks that would add gold. Any drops left over each replace one
      base restorative.
    * **Replacements.** Each vacated slot takes an item from the supply (see ``_replacement``),
      and the slots are filled in order, cheapest travelling drop first. A travelling weapon,
      armor or accessory stays out of the supply unless Hammer sells it, so no enemy drops it.
    """
    candidates = sorted(((key, slot.item) for key, slot in _VANILLA_SLOTS.items()
                         if _item_worth(slot.item) < worth.STRONG_WORTH
                         and (wide or _item_worth(slot.item) <= worth.CHEAP_WORTH)),
                        key=lambda pair: (_item_worth(pair[1]), pair[0]))
    checked = _checked_enemies(enemy_checks, goal)
    places = len(checked) + sum(count for _name, count in BASE_RESTORATIVES)
    moving = candidates[:places]

    supply = _supply(frozenset(item for _key, item in moving))
    used: Counter = Counter()
    replacements: Dict[SlotKey, str] = {}
    for key, item in moving:
        replacements[key] = _replacement(item, supply, used)
        used[replacements[key]] += 1

    # Each checked enemy adds its own travelling drop, from slot 1 when both slots travel.
    names = {enemy.id: enemy.name for enemy in enemy_type_collection}
    check_of: Dict[SlotKey, str] = {}
    for key, _item in sorted(moving):
        name = names[key[0]]
        if name in checked and name not in check_of.values():
            check_of[key] = name
    # The other drops, cheapest first, go to the checks that would add gold, then to the other
    # checks, and then replace base restoratives.
    gold_checks = {enemy.name for enemy in enemy_type_collection if not any(enemy.drops)}
    free_checks = [name for name in sorted(checked, key=lambda name: name not in gold_checks)
                   if name not in check_of.values()]
    base_items = [name for name, count in BASE_RESTORATIVES for _ in range(count)]
    travelling: List[TravellingDrop] = []
    for key, item in moving:
        check = check_of.get(key)
        base_item = None
        if check is None:
            if free_checks:
                check = free_checks.pop(0)
            else:
                base_item = base_items.pop(0)
        travelling.append(TravellingDrop(key[0], key[1], item, replacements[key], check, base_item))

    contents = {key: DropSlot(replacements.get(key, slot.item), slot.rarity)
                for key, slot in _VANILLA_SLOTS.items()}
    return TravelPlan(tuple(travelling), supply, MappingProxyType(contents))


def _hp_ranks() -> Dict[int, float]:
    """Return each enemy type's HP rank, which is the share of types whose HP is lower than its
    own."""
    hps = [enemy.hp for enemy in enemy_type_collection]
    return {enemy.id: sum(1 for hp in hps if hp < enemy.hp) / len(hps)
            for enemy in enemy_type_collection}


_HP_RANKS = _hp_ranks()


def deal(contents: Mapping[SlotKey, DropSlot],
         depths: Mapping[int, float]) -> Mapping[int, Tuple[DropSlot, DropSlot]]:
    """Return the whole drop table, with ``contents`` dealt so that the cheapest go to the
    lowest-level enemies.

    ``contents`` maps each filled slot to what it holds, and ``depths`` gives each enemy's seed
    depth by enemy id. An enemy's level is its seed depth plus HP_WEIGHT times its HP rank. An
    enemy missing from ``depths`` counts as unreached, at depth 1.0.

    The slots fall into four groups, by slot (common or rare) and by the kind of their vanilla
    item (equipment or consumable), and each group is dealt on its own. Within a group, the slots
    in order of level take the contents in order of worth. Each item keeps its rarity byte
    wherever it goes, and empty slots stay empty.
    """
    def level(key: SlotKey) -> Tuple[float, int]:
        enemy_id = key[0]
        depth = depths.get(enemy_id)
        if depth is None:
            depth = UNREACHED
        return depth + HP_WEIGHT * _HP_RANKS[enemy_id], enemy_id

    groups: Dict[Tuple[int, bool], List[SlotKey]] = {}
    for key in contents:
        groups.setdefault((key[1], _is_equipment(_VANILLA_SLOTS[key].item)), []).append(key)
    dealt: Dict[SlotKey, DropSlot] = {}
    for keys in groups.values():
        keys.sort(key=level)
        # Copies of one item tie on this key. sorted() is stable, so they are dealt in the level
        # order of the slots they came from, each with its own rarity byte.
        ordered = sorted((contents[key] for key in keys),
                         key=lambda content: (_item_worth(content.item), content.item))
        dealt.update(zip(keys, ordered))

    table = dict(vanilla_drop_table())
    for enemy_id, row in table.items():
        table[enemy_id] = tuple(dealt.get((enemy_id, slot), content)
                                for slot, content in enumerate(row, start=1))
    return MappingProxyType(table)


def plan_drop_shuffle(enemy_checks: int, goal: int, wide: bool,
                      depths: Mapping[int, float]) -> DropShufflePlan:
    """Return one seed's Item Drop Shuffle, given each enemy's seed depth by enemy id.

    Only the dealt table depends on ``depths``. Equal arguments give an equal plan.
    """
    travel = plan_travel(enemy_checks, goal, wide)
    return DropShufflePlan(travel, deal(travel.contents, depths))


# Each enemy id's two drop slots, common first.
DropTable = Mapping[int, Tuple[DropSlot, DropSlot]]


def _wide_item_smoothing(world: CVAOSWorld) -> bool:
    """Return whether Wide Item Smoothing is on. Wide moves more drops into the pool, and it also
    changes the final drop table."""
    return bool(world.options.wide_item_smoothing)


def travel_plan(world: CVAOSWorld) -> Optional[TravelPlan]:
    """Return the travel plan for ``world``'s options, or None when Item Drop Shuffle is off.

    The plan depends only on options that the slot data carries, so Universal Tracker's rebuild of
    the world gets the same pool.
    """
    options = world.options
    if not options.item_drop_shuffle:
        return None
    return plan_travel(options.enemy_checks.value, options.goal.value, _wide_item_smoothing(world))


def final_drop_table(world: CVAOSWorld) -> Optional[DropTable]:
    """Return the drop table that ``world``'s patch writes, or None when the seed keeps the game's
    own.

    The table starts as the one that Item Drop Shuffle deals by seed depth, or as the game's own
    table when that option is off. With Wide Item Smoothing on, ``wide_smoothing.band_drop_table``
    then moves each drop worth more than its enemy's seed depth plus ``wide_smoothing.BAND``.

    This function alone decides the final table, so any later step that changes drops belongs
    here. It reads the seed depths, so call it only after progression balancing.
    """
    options = world.options
    wide = _wide_item_smoothing(world)
    if not options.item_drop_shuffle and not wide:
        return None
    depths = enemy_seed_depths(world)
    if options.item_drop_shuffle:
        plan = plan_drop_shuffle(options.enemy_checks.value, options.goal.value, wide, depths)
        table, supply = plan.table, plan.travel.supply
    else:
        # No drop travels, so the supply holds every item a drop slot may take.
        table, supply = vanilla_drop_table(), _supply(frozenset())
    if not wide:
        return table
    from .wide_smoothing import band_drop_table
    return band_drop_table(table, depths, supply).table
