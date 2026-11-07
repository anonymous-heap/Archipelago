"""
Wide Item Smoothing: keep gear that is too good for the start of a run out of it, from every source.

The pass runs in ``stage_post_fill`` for every player with the ``wide_item_smoothing`` option on,
after the passes of ``item_smoothing`` that place the souls and books.

The order deals each player's items again among the locations that already hold them, earliest
location first. It covers the player's pickups, their defeat locations and their items in other
worlds. It moves the items that are not progression, and the weapons whose only progression use is
breaking floors or ceilings. Locked, excluded and priority locations keep their items, so plando
and early items stay where they are. A linked item belongs to its group, so it never moves either.

Items are ranked by worth (``data/item_balancing/worth.csv``), with the jitter of strict Item
Smoothing. An item is too early for a location when its worth exceeds the location's position by
more than ``BAND``. The order gives a location such an item only when no other remaining item fits
there.

Every placement passes the location's ``can_fill``, so item rules, Local Items and Non-Local Items
hold. When no remaining item fits a location, it trades with an earlier location that both accept
(see ``_trade``). If no such pair exists, the order is undone for that player.

Moving a breaker can change what logic reaches, so the order pins one of each kind (see ``_pin``).
Logic then reaches every location no later than before, and the breakers never need to go back.

Positions run from 0 to 1 on the player's own clock: the share of their pickup locations that the
seed's sphere order reaches first. The order reads the room clock, on which a defeat location counts
as reached once one of its enemy's rooms is. Logic reaches most defeat locations much later, through
the eight-soul fallback of enemies whose route is not recorded yet, while players make most of those
checks early.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

from BaseClasses import (CollectionState, Item, ItemClassification, Location, LocationProgressType,
                         MultiWorld)

from .data import item_balancing as desirability_data
from .data.item_balancing import worth
from .enemy_checks import enemy_id_for_location
from .item_smoothing import (PARAMS_BY_KEY, SmoothingParams, _UNREACHED_DEPTH, _accessible,
                             _category, _jittered, _region_depth, _restore_breakers)
from .items import logic_only_progression_names
from .locations import CVAOSLocation, pickup_location_ids
from .regions import _breakers

if TYPE_CHECKING:
    from . import CVAOSWorld

# An item is too early for a location when its worth exceeds the location's position by more than
# this.
BAND = 0.25


def promote_mid_game_gear(pool: List[Item]) -> None:
    """Make every filler weapon and armor in ``pool`` worth MID_GAME_WORTH or more useful.

    The order never deals to an excluded location, and the main fill may put filler on another
    player's excluded location. Useful items never land there, so the order can reach this gear
    wherever the fill puts it.
    """
    for item in pool:
        row = worth.by_name.get(item.name)
        if (row is not None and row.category in ("weapon", "armor")
                and row.worth >= worth.MID_GAME_WORTH and not (item.advancement or item.useful)):
            item.classification = ItemClassification.useful


def _worth(name: str) -> float:
    # Every item this world creates has a row. An item without one counts as worthless.
    row = worth.by_name.get(name)
    return 0.0 if row is None else row.worth


def _is_breaker(item: Item) -> bool:
    return item.advancement and item.name in logic_only_progression_names


def _is_defeat(location: Location) -> bool:
    return (isinstance(location, CVAOSLocation) and location.address is not None
            and location.address not in pickup_location_ids
            and enemy_id_for_location(location.address) is not None)


def _room_regions(location: Location) -> List:
    """The regions whose entrances lead to a defeat location: the rooms where its enemy appears."""
    return [entrance.parent_region for entrance in location.parent_region.entrances
            if entrance.parent_region is not None]


# --- clocks -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Walk:
    """One sweep of the multiworld's spheres."""

    sphere_of: Dict[Location, int]           # each filled location the sweep reaches, by sphere
    region_first: Dict[int, Dict[str, int]]  # player -> region name -> the sphere that reaches it


def walk(multiworld: MultiWorld, room_clock: bool) -> Walk:
    """Sweep the multiworld's spheres once, collecting each sphere's items before the next.

    On the room clock, a defeat location of this game counts as reached once one of its enemy's
    rooms is. Otherwise a location counts as reached when logic reaches it. Locations the sweep
    never reaches are left out.
    """
    state = CollectionState(multiworld)
    remaining = set(multiworld.get_filled_locations())
    sphere_of: Dict[Location, int] = {}
    region_first: Dict[int, Dict[str, int]] = {player: {} for player in multiworld.player_ids}
    index = 0
    while remaining:
        for player in multiworld.player_ids:
            state.update_reachable_regions(player)
            for region in state.reachable_regions[player]:
                region_first[player].setdefault(region.name, index)

        def reached(location: Location) -> bool:
            if room_clock and _is_defeat(location):
                regions = state.reachable_regions[location.player]
                return any(region in regions for region in _room_regions(location))
            return location.can_reach(state)

        sphere = {location for location in remaining if reached(location)}
        if not sphere:
            break
        for location in sphere:
            sphere_of[location] = index
            state.collect(location.item, True, location)
        remaining -= sphere
        index += 1
    return Walk(sphere_of, region_first)


class Clock:
    """One player's position in the run, from 0 to 1, for the locations of a walk.

    The player's pickup locations are ordered by sphere, then map distance, then name, and each one
    sits in the middle of its share of the run. Any other location sits at the middle of its sphere's
    pickups. A defeat location of the player takes the sphere where one of its enemy's rooms is
    first reached.
    """

    def __init__(self, multiworld: MultiWorld, player: int, sweep: Walk):
        self.player = player
        self.walk = sweep
        depth = _region_depth(multiworld, player)
        pickups = sorted((location for location in multiworld.get_locations(player)
                          if location.address in pickup_location_ids and location in sweep.sphere_of),
                         key=lambda location: (sweep.sphere_of[location],
                                               depth.get(location.parent_region.name, _UNREACHED_DEPTH),
                                               location.name))
        count = max(1, len(pickups))
        self._pickups = {location: (index + 0.5) / count for index, location in enumerate(pickups)}
        per_sphere = Counter(sweep.sphere_of[location] for location in pickups)
        self._middles: Dict[int, float] = {}
        before = 0
        for sphere in range(max(sweep.sphere_of.values(), default=0) + 1):
            self._middles[sphere] = (before + per_sphere[sphere] / 2) / count
            before += per_sphere[sphere]

    def at_sphere(self, sphere: Optional[int]) -> float:
        """The middle of ``sphere``'s pickups, or 1.0 for a sphere the walk never reaches."""
        return self._middles.get(sphere, 1.0)

    def defeat_sphere(self, location: Location) -> Optional[int]:
        """The first sphere that reaches one of the rooms of a defeat location's enemy."""
        first = self.walk.region_first[location.player]
        spheres = [first[region.name] for region in _room_regions(location) if region.name in first]
        return min(spheres, default=None)

    def position(self, location: Location) -> Optional[float]:
        """The location's position, or None when the walk never reaches it."""
        if location.player == self.player:
            if location in self._pickups:
                return self._pickups[location]
            if _is_defeat(location):
                sphere = self.defeat_sphere(location)
                return None if sphere is None else self.at_sphere(sphere)
        sphere = self.walk.sphere_of.get(location)
        return None if sphere is None else self.at_sphere(sphere)


# --- the order --------------------------------------------------------------------------------

@dataclass
class OrderResult:
    """What one order did for one player."""

    positions: Dict[Location, float]   # each location the order dealt to, at its position
    placed: Dict[Location, Item] = field(default_factory=dict)  # their items when it finished
    over_band: int = 0                 # items given above the band, because nothing else fit
    trades: int = 0                    # trades with an earlier location, by _trade
    undone: bool = False               # some location had no remaining item and no trade
    pinned: List[Location] = field(default_factory=list)  # the locations _pin gave a breaker
    # Each breaker the order dealt, with the location that held it before.
    breaker_homes: List[Tuple[Item, Location]] = field(default_factory=list)


@dataclass
class WideReport:
    """What Wide Item Smoothing did for one player."""

    orders: List[OrderResult] = field(default_factory=list)
    restores: int = 0                  # times the breakers went back to their homes


def _dealt_locations(multiworld: MultiWorld, player: int, clock: Clock) -> Dict[Location, float]:
    """Return the locations whose items the player's order deals, earliest first, with their
    positions.

    A location in another world sorts after the player's own locations in the same sphere, as Dark
    Souls III's smoothing orders them.
    """
    found = []
    for location in multiworld.get_filled_locations():
        item = location.item
        if (item.player != player or location.locked or location not in clock.walk.sphere_of
                or location.progress_type in (LocationProgressType.EXCLUDED,
                                              LocationProgressType.PRIORITY)
                or (item.advancement and not _is_breaker(item))):
            continue
        own = location.player == player
        if own and location.address in pickup_location_ids:
            position = clock.position(location)
        elif own and _is_defeat(location):
            position = clock.at_sphere(clock.defeat_sphere(location))
        elif not own:
            position = clock.at_sphere(clock.walk.sphere_of[location]) + 1e-6
        else:
            continue
        name = location.name if own else f"~{location.player}:{location.name}"
        found.append((position, name, location))
    found.sort(key=lambda entry: entry[:2])
    return {location: position for position, _name, location in found}


def _targets(items: List[Item], params: SmoothingParams, rng) -> List[float]:
    """Return a placement target for each item, in the order of ``items``.

    This is ``item_smoothing._assign_targets`` with worth in place of desirability. Gold is spread
    the same way: one copy of each amount but 100 Gold goes last, and the rest is spread evenly
    over the run. Every other item is ranked by worth into evenly spaced positions, ties in random
    order, and then jittered. No consumables are held for the second half, because worth already
    ranks restoratives by how much they restore.
    """
    golds = [index for index, item in enumerate(items) if _category(item.name) == "money"]
    rng.shuffle(golds)
    base: Dict[int, float] = {}
    scale: Dict[int, float] = {}
    banded: List[int] = []
    promoted: Counter = Counter()
    for index in golds:
        name = items[index].name
        scale[index] = params.sigma_base
        if name != "100 Gold" and not promoted[name]:
            base[index] = 2.0 + len(promoted)  # after every item, since worth is at most 1
            promoted[name] += 1
        else:
            banded.append(index)
    for index, item in enumerate(items):
        if index in scale:
            continue
        base[index] = _worth(item.name)
        info = desirability_data.by_name.get(item.name)
        scale[index] = params.sigma_base + (params.k * info.disagreement if info is not None else 0.0)
    ranked = sorted(base, key=lambda index: (base[index], rng.random()))
    denominator = max(1, len(ranked) - 1)
    position = {index: rank / denominator for rank, index in enumerate(ranked)}
    for slot, index in enumerate(banded):
        position[index] = rng.uniform(slot / len(banded), (slot + 1) / len(banded))
    return [_jittered(position[index], scale[index], params, rng) for index in range(len(items))]


def _take(pool: List[Item], fits: Callable[[Item], bool]) -> Optional[Item]:
    """Remove and return the first item in ``pool`` that ``fits``, or None."""
    for index, item in enumerate(pool):
        if fits(item):
            return pool.pop(index)
    return None


def _deal(locations: List[Location], pool: List[Item], positions: Dict[Location, float],
          state: CollectionState, result: OrderResult) -> bool:
    """Give each location, in order, the first item of ``pool`` that it accepts within the band.

    A location that accepts no item within the band takes the first one it accepts at all, and the
    result counts it. A location that accepts no remaining item trades with an earlier one (see
    ``_trade``). Return False when that fails too.
    """
    dealt: List[Location] = []
    for location in locations:
        limit = positions[location] + BAND
        item = _take(pool, lambda it: _worth(it.name) <= limit and location.can_fill(state, it, False))
        if item is None:
            item = _take(pool, lambda it: location.can_fill(state, it, False))
            result.over_band += item is not None
        if item is None:
            item = _trade(location, dealt, pool, positions, state, result)
        if item is None:
            return False
        location.item = item
        item.location = location
        dealt.append(location)
    return True


def _trade(location: Location, dealt: List[Location], pool: List[Item],
           positions: Dict[Location, float], state: CollectionState,
           result: OrderResult) -> Optional[Item]:
    """Find an item for a location that accepts no remaining item, and return it.

    The item comes from an earlier location that this one accepts, and the earlier location takes a
    remaining item that it accepts in exchange. Both checks use ``can_fill``. The latest earlier
    location is tried first, and a pair that keeps the earlier location within the band comes
    before any other. The item that moves here arrives no earlier than it would have, so it stays
    within the band if it was. Return None when no pair exists.
    """
    for within_band in (True, False):
        for earlier in reversed(dealt):
            item = earlier.item
            if not location.can_fill(state, item, False):
                continue
            limit = positions[earlier] + BAND
            replacement = _take(pool, lambda it: (not within_band or _worth(it.name) <= limit)
                                and earlier.can_fill(state, it, False))
            if replacement is None:
                continue
            earlier.item = replacement
            replacement.location = earlier
            result.trades += 1
            result.over_band += not within_band
            return item
    return None


def _pin(multiworld: MultiWorld, world: "CVAOSWorld", positions: Dict[Location, float],
         pool: List[Item], logic: Walk) -> List[Location]:
    """Keep a breaker where logic first finds each kind of the player's breakers.

    Logic asks only whether the player holds some ceiling breaker and some floor breaker. Among the
    locations holding the player's ceiling breakers, the one that logic reaches first keeps a
    ceiling breaker: the cheapest in ``pool`` that it accepts. The same goes for floor breakers, and
    one location may need both kinds. Logic then finds each kind no later than before, so it reaches
    every location no later than before.

    Each breaker placed leaves ``pool``. Return the locations that got one.
    """
    ceiling, floor = _breakers(world)
    held = [location for location in multiworld.get_filled_locations()
            if location.item.player == world.player and _is_breaker(location.item)]
    needs: Dict[Location, List[bool]] = {}
    for kind, names in enumerate((ceiling, floor)):
        homes = [location for location in held if location.item.name in names]
        if not homes:
            continue
        first = min(homes, key=lambda location: (logic.sphere_of.get(location, _UNREACHED_DEPTH),
                                                 positions.get(location, 1.0), location.name))
        # A location outside the order keeps its breaker anyway.
        if first in positions:
            needs.setdefault(first, [False, False])[kind] = True
    pinned = []
    for location, (needs_ceiling, needs_floor) in needs.items():
        fitting = [item for item in pool if _is_breaker(item)
                   and (not needs_ceiling or item.name in ceiling)
                   and (not needs_floor or item.name in floor)
                   and location.can_fill(multiworld.state, item, False)]
        if not fitting:
            continue
        item = min(fitting, key=lambda item: (_worth(item.name), item.name))
        del pool[next(index for index, other in enumerate(pool) if other is item)]
        location.item = item
        item.location = location
        pinned.append(location)
    return pinned


def _order_player(multiworld: MultiWorld, world: "CVAOSWorld", room: Walk,
                  logic: Walk) -> OrderResult:
    """Deal one player's items again among the locations that hold them.

    The items are sorted by their targets. ``_pin`` places its breakers first, and ``_deal`` hands
    out the rest. A pinned breaker above the band counts as such a placement. When the deal fails,
    every location gets back the item it held before.
    """
    positions = _dealt_locations(multiworld, world.player, Clock(multiworld, world.player, room))
    result = OrderResult(positions)
    locations = list(positions)
    before = {location: location.item for location in locations}
    result.breaker_homes = [(item, location) for location, item in before.items() if _is_breaker(item)]
    items = list(before.values())
    params = PARAMS_BY_KEY[world.options.item_smoothing.current_key]
    targets = _targets(items, params, world.random)
    pool = [items[index] for index in sorted(range(len(items)), key=targets.__getitem__)]
    result.pinned = _pin(multiworld, world, positions, pool, logic)
    result.over_band += sum(1 for location in result.pinned
                            if _worth(location.item.name) > positions[location] + BAND)
    pinned = set(result.pinned)
    rest = [location for location in locations if location not in pinned]
    if not _deal(rest, pool, positions, multiworld.state, result):
        for location, item in before.items():
            location.item = item
            item.location = location
        result.undone = True
        result.pinned = []
    result.placed = {location: location.item for location in locations}
    return result


def _order_round(multiworld: MultiWorld, worlds: List["CVAOSWorld"]) -> None:
    """Order every player in ``worlds`` once.

    One walk of the room clock gives every player's positions, and one walk of logic gives every
    player's pins. The pins keep logic from losing ground, so the check that follows is only a
    safety net: if the new placement breaks the multiworld's accessibility, the breakers go back to
    the locations they held before this round.
    """
    room = walk(multiworld, room_clock=True)
    logic = walk(multiworld, room_clock=False)
    homes: List[Tuple[Item, Location]] = []
    for world in worlds:
        result = _order_player(multiworld, world, room, logic)
        world.wide_report.orders.append(result)
        homes += result.breaker_homes
    if homes and not _accessible(multiworld):
        _restore_breakers(homes)
        for world in worlds:
            world.wide_report.restores += 1


# --- entry point ------------------------------------------------------------------------------

def run(multiworld: MultiWorld, game: str) -> None:
    """Run Wide Item Smoothing for every world of ``game`` with the option on."""
    worlds = [world for world in multiworld.get_game_worlds(game)
              if world.options.wide_item_smoothing]
    if not worlds:
        return
    for world in worlds:
        world.wide_report = WideReport()
    _order_round(multiworld, worlds)
