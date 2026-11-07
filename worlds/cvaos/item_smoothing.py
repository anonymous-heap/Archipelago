"""
Item smoothing: place less desirable items earlier in the run and more desirable items later.

Every pass here runs only when the ``item_smoothing`` option is on:

* The gating mobility souls (Malphas, Hippogryph and Giant Bat) and Black Panther get lateness
  targets, set by the ``early_mobility_souls`` and ``black_panther_bias`` options.
  ``bias_progression_souls`` places them near those targets before the main fill, and
  ``refine_soul_positions`` moves them closer afterward.
* The three Ancient Books are spread over the run. ``spread_ancient_books`` places them in early,
  middle and late bands of map depth before the main fill. ``refine_ancient_books`` then moves
  them toward an early, a middle and a mid-late target in sphere order.
* ``smooth_placed_items`` reorders items after the main fill. It moves non-progression items, and
  the weapons whose only progression use is breaking floors or ceilings, among the locations that
  already hold them. Less desirable items go to earlier locations, with long-tailed random jitter
  so that a lucky early find stays possible. The ``item_smoothing_order`` option decides whether
  "earlier" means nearer the start of the map (the default) or in an earlier logical sphere.

A world with Wide Item Smoothing on skips ``smooth_placed_items``, because ``wide_smoothing``
reorders its items instead. That module also uses several helpers from this one, such as
``region_depth``, ``jittered`` and ``restore_breakers``.

``take_smoothing_items`` holds the souls and books out of the multiworld pool in create_items.
The pre_fill passes place them, and any they cannot place go into the pool for the main fill.

Only pickup locations take part. Defeat locations (enemy checks) are left out, because most of them
come late in logic, and counting them would push every pickup's position earlier.

Every pass is logic-safe. The pre_fill passes place an item only where logic can reach it without
that item, and a post-fill swap of a soul or book is kept only if the whole multiworld stays
accessible. Reordering non-progression items among locations that already hold them never changes
reachability. Moving a floor or ceiling breaker can, so when the result fails the accessibility
check, the breakers go back to the locations the main fill gave them.
"""

from __future__ import annotations

import math
import re
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple, TYPE_CHECKING

from BaseClasses import CollectionState, Item, Location, LocationProgressType, MultiWorld

from .data import item_balancing as desirability_data
from .data import item_info
from .items import PROGRESSIVE_MOVEMENT, logic_only_progression_names
from .locations import pickup_location_ids

if TYPE_CHECKING:
    from . import CVAOSWorld


# --- tuning knobs -----------------------------------------------------------------------------

@dataclass(frozen=True)
class SmoothingParams:
    nu: float          # Student-t degrees of freedom, which set the tail weight. A lower value
                       # gives more extreme jumps, and an extreme early jump sorts to the front.
    sigma_base: float  # baseline jitter width (fraction of the run) every item gets
    k: float           # extra jitter width per unit of rater disagreement
    eps: float         # wildcard probability: ignore the target and place uniformly at random


# Keyed by the item_smoothing option's current_key. The jitter is added to an item's position in
# the run (see _assign_targets), so nu and sigma control how far through the run an item drifts.
# nu stays moderate so that an unusually early find lands somewhere in the early game, rather than
# always at the very first pickup.
PARAMS_BY_KEY: Dict[str, SmoothingParams] = {
    "loose":  SmoothingParams(nu=2.0, sigma_base=0.08, k=1.5, eps=0.05),
    "strict": SmoothingParams(nu=4.0, sigma_base=0.03, k=0.8, eps=0.02),
}

ANCIENT_BOOK_NAMES = ("Ancient Book 1", "Ancient Book 2", "Ancient Book 3")

# The three gating mobility souls get lateness targets, where 0 is the start of the run and 1 the
# end. These targets are separate from desirability, which rates all three souls highly.
BIASED_SOULS = ("Malphas", "Hippogryph", "Giant Bat")
# The biased souls that Progressive Movement copies stand in for.
PROGRESSIVE_STAND_INS = ("Malphas", "Giant Bat")

# Black Panther (dash) is aimed early by its own toggle, black_panther_bias, independent of the
# three souls above. The window and chance are constants rather than options. Logic sometimes
# leaves no early location for a gating soul, which caps how often Black Panther actually lands
# early whatever the chance is, so an on/off toggle is enough.
BLACK_PANTHER = "Black Panther"
BLACK_PANTHER_EARLY_WINDOW = 0.5   # aim into the first half of the run, by sphere order
BLACK_PANTHER_EARLY_CHANCE = 1.0   # the fraction of seeds that apply the bias

# Each soul's late target, as (lateness center, jitter spread), for runs where it is not the early
# soul. The centers are well past the early band, so only the designated soul lands early;
# otherwise several souls would often land early together.
SOUL_LATE_TARGETS = {
    "Malphas": (0.60, 0.16),     # double jump: only early when it's the designated pick
    "Hippogryph": (0.86, 0.13),  # high jump: generally late
    "Giant Bat": (0.92, 0.10),   # latest
}
# Under bias_early, each run has an EARLY_GUARANTEE chance that Malphas or Hippogryph is pulled
# early, so you have basic mobility in the first third. In the other ~7% of runs, all three keep
# their late targets. guarantee_early uses a chance of 1 instead. The early pick is weighted toward
# double jump. Giant Bat is never the early pick, because its flight reaches nearly everything the
# other two souls do. An early Giant Bat would open most of the castle at once.
EARLY_GUARANTEE = 0.93
EARLY_PICK_WEIGHTS = {"Malphas": 0.8, "Hippogryph": 0.2, "Giant Bat": 0.0}
EARLY_BAND = 0.33            # the early soul's target is drawn uniformly from the first third,
                             # rather than pinned to the very first pickup
SOUL_NU = 3.0                # moderate tail weight for the late soul targets

# Progression souls that gate movement. Which of these a location needs is a much better signal of
# how late it comes than its map distance, so _lateness_ranking builds its ability tiers from them.
# A location that needs none of them is reachable with base abilities only.
MOVEMENT_SOULS = frozenset({
    "Flying Armor", "Giant Bat", "Black Panther", "Undine", "Skula",
    "Grave Keeper", "Skeleton Blaze", "Malphas", "Hippogryph", "Galamoth",
    PROGRESSIVE_MOVEMENT,  # each copy adds the next of Flying Armor, Malphas, Giant Bat
})

ORIGIN_REGION = "Menu"
UNREACHED_DEPTH = 10 ** 9  # past every real depth or sphere, so unreached ones sort last

_GOLD = re.compile(r"^\d+ Gold$")


# --- item identity ----------------------------------------------------------------------------

def category(name: str) -> str:
    """
    Return the category of the item named ``name``.

    It is the item's ``item_info`` category, ``"money"`` for a gold amount, or ``"unknown"`` for
    any other name.
    """
    info = item_info.by_name.get(name)
    if info is not None:
        return info.item_category
    if _GOLD.match(name):
        return "money"
    return "unknown"


# --- target-position model --------------------------------------------------------------------

def _student_t(nu: float, rng) -> float:
    """
    Return a standard Student-t draw with ``nu`` degrees of freedom.

    A large ``nu`` approaches a Gaussian, and a smaller ``nu`` gives heavier tails. ``nu = 1`` is
    the Cauchy distribution.
    """
    chi2 = rng.gammavariate(nu / 2.0, 2.0)  # chi-squared with nu d.o.f.
    if chi2 <= 0.0:  # gammavariate can underflow to 0.0 for small shape; avoid a divide-by-zero
        return 0.0
    return rng.gauss(0.0, 1.0) / math.sqrt(chi2 / nu)


def jittered(base: float, scale: float, params: SmoothingParams, rng) -> float:
    """
    Return ``base`` plus long-tailed jitter of width ``scale``.

    The jitter is a Student-t draw with ``params.nu`` degrees of freedom. With probability
    ``params.eps``, the result is instead a uniform random wildcard in [0, 1].
    """
    if rng.random() < params.eps:
        return rng.random()
    return base + scale * _student_t(params.nu, rng)


def _assign_targets(items: List[Item], params: SmoothingParams, rng) -> List[float]:
    """Return a placement target for each item, in the order of ``items``.

    A target is a position in the run, from 0 (first) to 1 (last), plus random jitter. The placement
    pass sorts items by target, so a higher target means a later location. Copies of an item share
    its name, and items compare equal by name and player, so this function tracks each item by its
    index in ``items``.

    Most items get their position in two steps:

    1. Each item gets a base value. A rated item's base is its desirability, and an unrated item's
       base is drawn uniformly from [0, 1]. One copy of each gold amount except 100 Gold gets a base
       above every rated item, so these copies take the last positions in the run.
    2. The bases are rank-transformed into evenly spaced positions in [0, 1].

    Two groups skip both steps and get a position directly. The rest of the gold is spread evenly
    over the whole run, because its desirability is near zero and ranking it would put all of it at
    the start. A few copies of some mid-tier consumables are spread evenly over the second half.

    The jitter is then added to the position. It is a Student-t draw with ``nu`` degrees of freedom,
    scaled by the item's jitter width. That width is ``sigma_base``, plus ``k`` times the rater
    disagreement for a rated item. With probability ``eps``, an item gets a uniformly random target
    instead. Targets are not clamped to [0, 1], because the placement pass uses only their order,
    so an extreme draw just gives an extreme rank.

    The jitter goes on the position rather than on the desirability because desirability values are
    packed densely at the low end and sparsely at the top. Jittered in desirability, a top item
    would have to cross the whole dense group, so a small draw would barely move it and a large one
    would send it to the very front. Positions are evenly spaced, so a moderate draw moves an item a
    moderate fraction of the run.
    """
    base: Dict[int, float] = {}
    scale: Dict[int, float] = {}

    # Gold is placed by copy rather than by desirability, so the money rows in desirability.csv are
    # not used here.
    golds = [i for i, it in enumerate(items) if category(it.name) == "money"]
    rng.shuffle(golds)

    # One copy of each larger gold amount gets a base above every rated item, so it lands after the
    # best equipment. 100 Gold is worth too little for that, so none of it is promoted. Every other
    # copy is spread over the whole run below.
    gold_uppermost_quota = {"100 Gold": 0}          # an amount not listed has one copy promoted
    top_base = max((info.desirability for info in desirability_data.by_name.values()), default=1.0)
    promoted_count: Dict[str, int] = {}
    promoted_rank = 0
    banded: List[int] = []
    for i in golds:
        name = items[i].name
        if promoted_count.get(name, 0) < gold_uppermost_quota.get(name, 1):
            promoted_count[name] = promoted_count.get(name, 0) + 1
            base[i] = top_base + 1.0 + promoted_rank  # strictly above all rated items
            scale[i] = params.sigma_base
            promoted_rank += 1
        else:
            scale[i] = params.sigma_base
            banded.append(i)
    banded_set = set(banded)

    # Spread a fixed number of copies of some mid-tier consumables over the second half of the run
    # (positions 0.5 to 1.0) instead of placing them by desirability, so that not all the healing
    # and mana items show up early. The rng picks which copies, and the others are ranked as usual.
    second_half_quota = {"High Potion": 2, "Super Potion": 3, "Mana Prism": 4, "High Mind Up": 1}
    second_half: List[int] = []
    taken: Dict[str, int] = {}
    candidates = [i for i, it in enumerate(items) if it.name in second_half_quota]
    rng.shuffle(candidates)
    for i in candidates:
        name = items[i].name
        if taken.get(name, 0) < second_half_quota[name]:
            taken[name] = taken.get(name, 0) + 1
            scale[i] = params.sigma_base
            second_half.append(i)
    second_half_set = set(second_half)

    for i, item in enumerate(items):
        if i in base or i in banded_set or i in second_half_set:
            continue
        info = desirability_data.by_name.get(item.name)
        if info is not None:
            base[i] = info.desirability
            scale[i] = params.sigma_base + params.k * info.disagreement
        else:
            base[i] = rng.random()  # unranked: uniform prior over the whole run
            scale[i] = params.sigma_base

    # Rank-transform the bases into evenly spaced positions, preserving their order. The spread gold
    # and the second-half consumables are left out and get their positions directly below. Leaving
    # the gold out also keeps it from filling the lowest ranks and pushing every other item later.
    order = sorted((i for i in range(len(items)) if i not in banded_set and i not in second_half_set),
                   key=lambda i: base[i])
    denom = max(1, len(order) - 1)
    position = {i: rank / denom for rank, i in enumerate(order)}

    gold_count = len(banded)
    for index, i in enumerate(banded):
        lo = index / gold_count
        hi = (index + 1) / gold_count
        position[i] = rng.uniform(lo, hi)

    # Selected consumables: stratify evenly across the back half of the run [0.5, 1.0].
    sh_count = len(second_half)
    for index, i in enumerate(second_half):
        lo = 0.5 + (index / sh_count) * 0.5
        hi = 0.5 + ((index + 1) / sh_count) * 0.5
        position[i] = rng.uniform(lo, hi)

    targets: List[float] = []
    for i in range(len(items)):
        if rng.random() < params.eps:
            targets.append(rng.random())  # wildcard: ignore the target, place anywhere
        else:
            targets.append(position[i] + scale[i] * _student_t(params.nu, rng))
    return targets


# --- shared progress ordering -----------------------------------------------------------------

def region_depth(multiworld: MultiWorld, player: int) -> Dict[str, int]:
    """
    Return the breadth-first distance of each of the player's regions from ``ORIGIN_REGION``, by
    region name, ignoring access rules.

    A region that no path of exits from the origin reaches is left out.
    """
    root = multiworld.get_region(ORIGIN_REGION, player)
    depth: Dict[str, int] = {root.name: 0}
    queue = deque([root])
    while queue:
        region = queue.popleft()
        next_depth = depth[region.name] + 1
        for exit_ in region.exits:
            target = exit_.connected_region
            if target is not None and target.name not in depth:
                depth[target.name] = next_depth
                queue.append(target)
    return depth


def smoothable_locations_in_order(multiworld: MultiWorld, player: int, spheres: List,
                                  depth: Dict[str, int], *, by_map_distance: bool = False) -> List[Location]:
    """
    Return the locations whose items ``smooth_placed_items`` may move, earliest first.

    A location qualifies when it is one of this player's reachable pickup locations, it holds one of
    this player's own items, and that item is either non-progression or a weapon whose only
    progression use is breaking floors or ceilings. Locked, excluded and priority locations are left
    out.

    The caller sets ``by_map_distance`` from the ``item_smoothing_order`` option:

    * ``False``: ordered by ``(sphere, map depth, name)``. This is logical reachability order in
      this seed's real spheres, so it is seed-specific.
    * ``True``: ordered by ``(map depth, name)``. This is structural distance from the start in the
      unrandomized map, so it is seed-independent.

    Both orders cover the same locations and differ only in their order.

    Only this world's own locations take part, because a cvaos item sent to another world is not
    gear its recipient can use, so ordering it there has no meaning. This also makes the order a
    total order, since this world's location names are unique, and it respects local and non-local
    item settings without extra work. Excluded locations are left out because they may hold only
    filler, so moving a ``useful`` item into one would be illegal. Priority locations are left out
    because the ceiling and floor breakers are progression, so moving one out of a priority location
    could leave that location with no progression item.
    """
    # get_spheres yields the reachable spheres. When some filled locations are unreachable, it then
    # yields an empty set, followed by one set of all of them. Stopping at the empty set keeps
    # unreachable locations out of smoothing under minimal accessibility.
    sphere_of: Dict[Location, int] = {}
    for index, sphere in enumerate(spheres):
        if not sphere:
            break
        for loc in sphere:
            sphere_of[loc] = index
    candidates = [loc for loc in sphere_of
                  if loc.item is not None
                  and loc.address in pickup_location_ids
                  and loc.player == player
                  and loc.item.player == player
                  and loc.parent_region is not None
                  and not loc.locked
                  and loc.progress_type not in (LocationProgressType.EXCLUDED,
                                                LocationProgressType.PRIORITY)
                  and (not loc.item.advancement
                       or loc.item.name in logic_only_progression_names)]
    if by_map_distance:
        candidates.sort(key=lambda loc: (depth.get(loc.parent_region.name, UNREACHED_DEPTH), loc.name))
    else:
        candidates.sort(key=lambda loc: (sphere_of[loc],
                                         depth.get(loc.parent_region.name, UNREACHED_DEPTH), loc.name))
    return candidates


# --- hold back the items the pre_fill passes place (create_items, pre_fill) -------------------

def take_smoothing_items(world: "CVAOSWorld", itempool: List[Item]) -> List[Item]:
    """
    Remove and return the items in ``itempool`` that the pre_fill passes place.

    Items that pre_fill places must not be in the multiworld pool, and create_items is the last step
    that may change the pool, so they are removed here. The world returns them from
    ``get_pre_fill_items`` until they are placed, so ``get_all_state`` still collects them. Any that
    pre_fill cannot place go into the pool for the main fill (see ``_add_to_pool``).

    Nothing is taken when ``item_smoothing`` is off. Otherwise the Ancient Books are taken. Unless
    ``early_mobility_souls`` is off, the three gating souls are taken too. In that case, with
    Progressive Movement on, the three Progressive Movement copies are taken as well, because two
    of them stand in for Malphas and Giant Bat. Black Panther is taken when ``black_panther_bias``
    is on and this run's ``BLACK_PANTHER_EARLY_CHANCE`` roll, made here, succeeds.

    An item that the core needs in the pool for an item link, a plando or ``non_local_items`` is
    left there (see ``_names_used_elsewhere``). Nothing at all is taken when holding these items
    back would leave too few progression items for the priority locations (see
    ``_starves_priority_locations``).
    """
    options = world.options
    if options.item_smoothing.current_key == "off":
        return []
    names = set(ANCIENT_BOOK_NAMES)
    if options.early_mobility_souls.current_key != "off":
        names |= {*BIASED_SOULS, PROGRESSIVE_MOVEMENT}
    if options.black_panther_bias.value and world.random.random() < BLACK_PANTHER_EARLY_CHANCE:
        names.add(BLACK_PANTHER)
    names -= _names_used_elsewhere(world)
    taken = [item for item in itempool if item.name in names]
    if _starves_priority_locations(world, itempool, taken):
        return []
    itempool[:] = [item for item in itempool if item.name not in names]
    return taken


def _starves_priority_locations(world: "CVAOSWorld", itempool: List[Item],
                                taken: List[Item]) -> bool:
    """
    Whether holding ``taken`` back would leave the main fill fewer progression items than this
    world has priority locations.

    The main fill puts progression into priority locations before anything else, and generation
    fails if it runs out of progression items for them. The pre-fill passes place their items by
    lateness instead, so when the priority locations need nearly every progression item,
    smoothing leaves its items in the pool.
    """
    options = world.options
    existing = {location.name for location in world.multiworld.get_locations(world.player)}
    priority = (options.priority_locations.value - options.exclude_locations.value) & existing
    progression = sum(1 for item in itempool if item.advancement)
    held = sum(1 for item in taken if item.advancement)
    return len(priority) > progression - held


def _names_used_elsewhere(world: "CVAOSWorld") -> Set[str]:
    """
    Return the names of this player's items that other generation steps need in the pool.

    Before pre_fill, the core replaces linked items with their group's item and places plando'd
    items, taking both from the pool. It also keeps ``non_local_items`` out of this world's
    locations, so only the main fill can place them. Smoothing leaves all of these in the pool.
    Item group names are expanded to the items they contain.
    """
    names: Set[str] = set(world.options.non_local_items.value)
    for group in world.multiworld.groups.values():
        if world.player in group["players"]:
            names |= set(group["item_pool"])
    for block in world.options.plando_items.value:
        names |= set(block.items)
    expanded: Set[str] = set()
    for name in names:
        expanded |= set(world.item_name_groups.get(name, {name}))
    return expanded


def _all_state_except(multiworld: MultiWorld, unplaced: List[Item]) -> CollectionState:
    """
    The same state as ``multiworld.get_all_state()``, without the pre-fill items in ``unplaced``.

    ``bias_progression_souls`` calls this with every soul it has not placed yet, including the one
    it is about to place. That soul then cannot land behind itself, or behind a soul placed after
    it. The Ancient Books, which the next pass places, are still counted. ``spread_ancient_books``
    calls this with the books it places, so no book lands behind any of them.
    """
    state = multiworld.get_all_state(collect_pre_fill_items=False, perform_sweep=False)
    held_back = {id(item) for item in unplaced}
    for player in multiworld.player_ids:
        world = multiworld.worlds[player]
        for item in world.get_pre_fill_items():
            if id(item) not in held_back:
                world.collect(state, item)
    state.sweep_for_advancements()
    return state


def _add_to_pool(world: "CVAOSWorld", item: Item) -> None:
    """
    Add an item that a pre-fill pass could not place to the multiworld pool, for the main fill.

    This is the only time smoothing adds to the multiworld pool after create_items. It happens
    when a pass finds no free location that logic can reach without the item, or returns before
    placing it. The item is also removed from the world's pre-fill items, so ``get_all_state``
    does not collect it twice.
    """
    world._smoothing_items = [other for other in world._smoothing_items if other is not item]
    world.multiworld.itempool.append(item)


def add_unplaced_to_pool(world: "CVAOSWorld") -> None:
    """
    Add every held-back item that the pre_fill passes left unplaced to the multiworld pool.

    A pass can return before placing anything, for example when every location of this world is
    excluded, so the world calls this after both passes.
    """
    for item in world.get_pre_fill_items():
        _add_to_pool(world, item)


# --- pass 0: bias the gating mobility souls (pre_fill) ----------------------------------------

def _lateness_ranking(multiworld: MultiWorld, player: int, depth: Dict[str, int]) -> Dict[Location, float]:
    """
    Return each location's lateness in [0, 1], an estimate of how late in the run it is reached.

    Lateness comes from each location's ability tier (see ``_soul_tiers``). The tier is the step at
    which the location first becomes reachable when this player's movement souls are collected one
    at a time, most useful first, so a location at tier 0 needs no movement soul. Map depth breaks
    ties within a tier, and the ranked locations are then scaled to [0, 1].

    The tiers do not depend on where items are placed, so they work before the main fill. They are
    also a much better signal than map depth alone, which would rank a distant room with no soul
    gate as late and a nearby gated room as early. Computing them takes two sweeps per soul, and
    this runs once per world in pre_fill.
    """
    world = multiworld.worlds[player]
    # The souls that smoothing places are not in the multiworld pool yet, so add this world's
    # pre-fill items.
    pool = [*multiworld.itempool, *world.get_pre_fill_items()]
    souls = [item for item in pool if item.player == player and item.name in MOVEMENT_SOULS]
    # Excluded locations are left out, so the pre-fill bias never puts a soul in one.
    locs = [loc for loc in multiworld.get_locations(player)
            if loc.address in pickup_location_ids and loc.parent_region is not None
            and loc.parent_region.name in depth
            and loc.progress_type != LocationProgressType.EXCLUDED]

    tier = _soul_tiers(multiworld, player, pool, souls, locs)
    locs.sort(key=lambda loc: (tier[loc], depth[loc.parent_region.name], loc.name))
    denom = max(1, len(locs) - 1)
    return {loc: i / denom for i, loc in enumerate(locs)}


def _soul_tiers(multiworld: MultiWorld, player: int, pool: List[Item], souls: List[Item],
                locs: List[Location]) -> Dict[Location, int]:
    """
    Return each location's ability tier for ``_lateness_ranking``.

    The state starts with every item in ``pool`` except ``souls``, then collects the souls one at a
    time, starting with the one that makes the most locations reachable on its own. A location's
    tier is the step at which it first becomes reachable: 0 with no soul, and one past the last
    step if it never does. Each soul goes in through
    ``CollectionState.collect``, which marks the cached reachability stale, so the next
    ``can_reach`` accounts for it.
    """
    held_out = {id(soul) for soul in souls}
    base = CollectionState(multiworld)
    for item in pool:
        if id(item) not in held_out:
            multiworld.worlds[item.player].collect(base, item)
    base.sweep_for_advancements()

    def solo_reach(soul: Item) -> int:
        trial = base.copy()
        trial.collect(soul, prevent_sweep=True)
        trial.sweep_for_advancements()
        return sum(1 for loc in locs if loc.can_reach(trial))

    # The soul that makes the most locations reachable goes first. The name breaks ties, so the
    # order does not depend on the order of the item pool.
    ordered = sorted(souls, key=lambda soul: (-solo_reach(soul), soul.name))

    state = base
    tier: Dict[Location, int] = {}
    step = 0
    for soul in [None, *ordered]:  # step 0 = no souls, then one soul per step
        if soul is not None:
            step += 1
            state.collect(soul, prevent_sweep=True)
            state.sweep_for_advancements()
        for loc in locs:
            if loc not in tier and loc.can_reach(state):
                tier[loc] = step
    for loc in locs:
        tier.setdefault(loc, step + 1)  # never reachable -> latest
    return tier


def _designate_early_soul(rng, names: List[str], guarantee: float) -> Optional[str]:
    """Return the soul to place in the first third of the run, or None.

    With probability ``guarantee``, the soul is drawn from ``names`` using ``EARLY_PICK_WEIGHTS``.
    Otherwise this returns None, so every soul keeps its late target. It also returns None when no
    soul in ``names`` has a positive weight, as when plando or an item link leaves only Giant Bat.
    """
    if rng.random() >= guarantee:
        return None
    eligible = [name for name in names if EARLY_PICK_WEIGHTS[name] > 0]
    if not eligible:
        return None
    return rng.choices(eligible, weights=[EARLY_PICK_WEIGHTS[name] for name in eligible])[0]


def bias_progression_souls(world: "CVAOSWorld") -> None:
    """
    Place the gating mobility souls and Black Panther near lateness targets before the main fill.

    The mobility souls are Malphas, Hippogryph and Giant Bat. Each gets a target between 0 (the
    start of the run) and 1 (the end). The ``early_mobility_souls`` option decides whether one of
    them is aimed early: ``off`` skips all three, ``bias_early`` aims one early in an
    ``EARLY_GUARANTEE`` fraction of runs, and ``guarantee_early`` does so in every run.

    Black Panther is independent. When ``take_smoothing_items`` held it back, which depends on
    ``black_panther_bias``, it is aimed into the first ``BLACK_PANTHER_EARLY_WINDOW`` of the run.

    These souls gate regions, so the post-fill smoothing cannot move them freely. Instead, each one
    goes to the reachable location whose lateness (see ``_lateness_ranking``) is nearest its target.
    After the main fill, ``refine_soul_positions`` moves each soul closer to its target in real
    sphere order.

    The items are placed in order of target, earliest first, and reachability is recomputed before
    each one. A later item may therefore sit behind an earlier one, but never behind itself or an
    item placed after it. ``fill_restrictive`` places an item only where it is reachable. If nothing
    fits, the item goes into the multiworld pool for the main fill to place, so the bias can never
    make a seed unbeatable.

    With Progressive Movement on, the three Progressive Movement copies stand in for Malphas and
    Giant Bat. Two of them get targets drawn the way Malphas's is, and the third gets one drawn the
    way Giant Bat's is. ``refine_soul_positions`` does not move the copies afterwards.
    """
    from Fill import fill_restrictive

    multiworld = world.multiworld
    player = world.player
    rng = world.random

    # Candidate items: the three gating souls and Black Panther, but only those take_smoothing_items
    # kept out of the pool. Each gets a lateness target in [0, 1] (0 = earliest by sphere ordering,
    # 1 = latest), and all of them are then placed.
    found: Dict[str, Item] = {}
    for item in world.get_pre_fill_items():
        if item.name not in found and (item.name in BIASED_SOULS or item.name == BLACK_PANTHER):
            found[item.name] = item

    # A list of (item, target) pairs rather than a dict: items compare equal by name and player,
    # so the three Progressive Movement copies would collapse into one dict key.
    targets: List[Tuple[Item, float]] = []

    # The three mobility souls, gated by early_mobility_souls ("off" skips them entirely). Designate
    # the run's early-mobility soul (or none), then give each a lateness target: the designated one
    # gets a target in the first third, and the rest get late targets.
    mode = world.options.early_mobility_souls.current_key
    present = [found[name] for name in BIASED_SOULS if name in found]
    # With Progressive Movement on, three copies replace Flying Armor, Malphas and Giant Bat in the
    # pool. The second copy obtained grants Malphas and the third grants Giant Bat, so the copies
    # stand in for those souls when the early soul is chosen and the targets are set.
    copies = [item for item in world.get_pre_fill_items() if item.name == PROGRESSIVE_MOVEMENT]
    if mode != "off" and (present or copies):
        guarantee = 1.0 if mode == "guarantee_early" else EARLY_GUARANTEE
        names = [name for name in BIASED_SOULS
                 if name in found or (copies and name in PROGRESSIVE_STAND_INS)]
        designated = _designate_early_soul(rng, names, guarantee)

        def soul_target(name: str) -> float:
            if name == designated:
                return rng.uniform(0.0, EARLY_BAND)  # spread across the early region, not pinned to #1
            center, spread = SOUL_LATE_TARGETS[name]
            return min(1.0, max(0.0, center + spread * _student_t(SOUL_NU, rng)))

        for item in present:
            targets.append((item, soul_target(item.name)))
        # Two copies draw targets as Malphas would and the third as Giant Bat would. When Malphas
        # is the early soul, both Malphas-like copies target the early band, so the second copy
        # obtained (double jump) lands early.
        for item, stand_in in zip(copies, ("Malphas", "Malphas", "Giant Bat")):
            targets.append((item, soul_target(stand_in)))

    # Black Panther is present only when black_panther_bias is on and this run's
    # BLACK_PANTHER_EARLY_CHANCE roll succeeded, both checked in take_smoothing_items. Its target
    # falls in the first BLACK_PANTHER_EARLY_WINDOW of the run (same seed/sphere-ordering axis as
    # the souls above).
    panther = found.get(BLACK_PANTHER)
    if panther is not None:
        targets.append((panther, rng.uniform(0.0, BLACK_PANTHER_EARLY_WINDOW)))

    if not targets:
        return

    depth = region_depth(multiworld, player)
    lateness = _lateness_ranking(multiworld, player, depth)
    # The placement below compares each target with the pre-fill lateness. The post-fill refine
    # pass later moves each item toward the same target in sphere order, so the targets are stored.
    # _soul_targets is keyed by item name and the copies share a name, so the refine pass skips
    # them and their pre-fill placement stands.
    world._soul_targets = {item.name: target for item, target in targets
                           if item.name != PROGRESSIVE_MOVEMENT}

    # Place the earliest target first, and rebuild the state before each item. A later item can then
    # land behind an earlier one that is already placed, but never behind itself or a later one.
    for item, target in sorted(targets, key=lambda pair: pair[1]):
        unplaced = [other for other, _target in targets if other.location is None]
        base_state = _all_state_except(multiworld, unplaced)
        candidates = [loc for loc in lateness if loc.item is None and not loc.locked]
        candidates.sort(key=lambda loc: (abs(lateness[loc] - target), loc.name))
        # One item per call, because each item has its own candidate order, sorted by distance from
        # its own target. With a single item, fill_restrictive also has no earlier placement to swap
        # with, so it never moves a soul that is already placed. Don't batch souls into one call.
        pool = [item]
        fill_restrictive(multiworld, base_state, candidates, pool,
                         single_player_placement=True, lock=True, allow_partial=True)
        if pool:  # nothing reachable fit its target -> let the normal fill place it
            _add_to_pool(world, item)


# --- pass 0b: refine the souls and books against the real spheres (stage_post_fill) -----------

SOUL_REFINE_TRIES = 8  # post-fill swap candidates to try per soul before leaving it where it is


def _run_position(multiworld: MultiWorld, player: int) -> Dict[Location, float]:
    """
    Return how far through this player's run each filled pickup location comes, from 0 to 1.

    The order is by multiworld sphere, then map depth, then name. That is the real position in
    this seed, and it needs the spheres, so it is available only after the main fill. The refine
    passes use this order whatever ``item_smoothing_order`` is set to.
    """
    depth = region_depth(multiworld, player)
    locs = [loc for loc in multiworld.get_locations(player)
            if loc.address in pickup_location_ids and loc.item is not None
            and loc.parent_region is not None]
    spheres = list(multiworld.get_spheres())
    sphere_of = {loc: i for i, sphere in enumerate(spheres) for loc in sphere}
    locs.sort(key=lambda loc: (sphere_of.get(loc, UNREACHED_DEPTH),
                               depth.get(loc.parent_region.name, UNREACHED_DEPTH), loc.name))
    denom = max(1, len(locs) - 1)
    return {loc: i / denom for i, loc in enumerate(locs)}


def accessible(multiworld: MultiWorld) -> bool:
    """
    Return whether the whole multiworld fulfills its accessibility settings.

    ``fulfills_accessibility`` raises FillError on a dead end under ``__debug__`` instead of
    returning False, so this catches it. The check uses a fresh state, so it never changes
    placement.
    """
    from Fill import FillError
    try:
        return bool(multiworld.fulfills_accessibility())
    except FillError:
        return False


def _state_without(multiworld: MultiWorld, own_loc: Location) -> CollectionState:
    """The state a player reaches with every placed item except the one at ``own_loc``.

    It starts from the starting items and sweeps every other filled location, so an item
    reachable only through the item at ``own_loc`` is not counted either. A location reachable in
    this state does not need the item at ``own_loc``, so moving that item there cannot strand it
    behind itself.
    """
    state = CollectionState(multiworld)
    state.sweep_for_advancements(
        locations=[loc for loc in multiworld.get_filled_locations() if loc is not own_loc])
    return state


def _swap_allowed(own_loc: Location, dest: Location) -> bool:
    """Whether swapping the progression item at ``own_loc`` with the item at ``dest`` respects the
    player's location settings and each location's item rule.

    Progression never moves into an excluded location, and a priority location never gives up its
    progression item for a non-progression one.
    """
    return (dest.progress_type != LocationProgressType.EXCLUDED
            and own_loc.progress_type != LocationProgressType.PRIORITY
            and dest.item_rule(own_loc.item) and own_loc.item_rule(dest.item))


def _swap_if_safe(multiworld: MultiWorld, soul_loc: Location, dest: Location) -> bool:
    """
    Swap the items at ``soul_loc`` and ``dest``, and keep the swap only if the whole multiworld
    still fulfills accessibility. Return whether the swap was kept.

    Both refine passes use this, for a soul or for a book. A kept swap locks ``dest`` and unlocks
    ``soul_loc``, so the post-fill smoothing may move the item that now sits there.
    """
    soul, other = soul_loc.item, dest.item
    soul_loc.item, dest.item = other, soul
    soul.location, other.location = dest, soul_loc
    if accessible(multiworld):
        soul_loc.locked, dest.locked = False, True
        return True
    soul_loc.item, dest.item = soul, other  # revert
    soul.location, other.location = soul_loc, dest
    return False


def refine_soul_positions(world: "CVAOSWorld") -> None:
    """
    Move each biased soul closer to the target ``bias_progression_souls`` chose, using the real
    spheres after the main fill.

    A soul moves by swapping places with one of this player's non-progression items. Only locations
    reachable without the soul are candidates, so it is never placed behind itself. A swap must
    also keep progression out of excluded locations and in priority locations. Up to
    ``SOUL_REFINE_TRIES`` candidates nearest the target are tried, and a swap is kept only if the
    whole multiworld still fulfills accessibility, so it can never make a seed unbeatable. A soul
    that can't be moved safely stays where it is.

    This runs before ``smooth_placed_items``, which computes the spheres again.
    """
    targets = getattr(world, "_soul_targets", None)
    if not targets:
        return
    multiworld = world.multiworld
    player = world.player
    # Earliest target first: a later soul is then positioned against spheres that already reflect the
    # earlier ones (the order the player actually collects them).
    for name in sorted(targets, key=targets.get):
        soul_loc = next((loc for loc in multiworld.get_locations(player)
                         if loc.item is not None and loc.item.player == player
                         and loc.item.name == name), None)
        if soul_loc is None:  # soul wound up off-world (rare fallback) -> leave it
            continue
        without_soul = _state_without(multiworld, soul_loc)
        position = _run_position(multiworld, player)
        target = targets[name]
        candidates = sorted(
            (loc for loc in position if loc is not soul_loc and loc.item is not None
             and loc.item.player == player and not loc.item.advancement and not loc.locked
             and _swap_allowed(soul_loc, loc) and loc.can_reach(without_soul)),
            key=lambda loc: (abs(position[loc] - target), loc.name))
        for dest in candidates[:SOUL_REFINE_TRIES]:
            if _swap_if_safe(multiworld, soul_loc, dest):
                break


BOOK_REFINE_TRIES = 12  # post-fill swap candidates to try per book before leaving it where it is


def refine_ancient_books(world: "CVAOSWorld") -> None:
    """
    Move the three Ancient Books toward an early, a middle and a mid-late target, after the main
    fill.

    The targets are drawn from the first third, the middle third, and the band from 1/2 to 3/4 of
    the run, in sphere order. The spheres exist only after the main fill, so
    ``spread_ancient_books`` can place the books only roughly, by map depth, and this pass finishes
    the job.

    Each book moves the way ``refine_soul_positions`` moves a soul. It swaps places with one of this
    player's non-progression items at a location reachable without that book, and the swap is kept
    only if the whole multiworld still fulfills accessibility. These checks matter because the
    books are progression: the gate on the way to Chaos needs all three. A book that can't be moved
    safely stays where it is, and a book that plando locked in place is never moved.

    This runs before ``smooth_placed_items``.
    """
    multiworld = world.multiworld
    player = world.player
    rng = world.random
    # A locked book that spread_ancient_books did not place was put there on purpose (plando), so it
    # stays. Books the main fill placed are unlocked and may move.
    spread = {id(book) for book in getattr(world, "_spread_books", ())}
    book_locs = [loc for loc in multiworld.get_locations(player)
                 if loc.item is not None and loc.item.player == player
                 and loc.item.name in ANCIENT_BOOK_NAMES
                 and (not loc.locked or id(loc.item) in spread)]
    if not book_locs:
        return

    # One target in the early third [0, 1/3], one in the middle third [1/3, 2/3], and one in the
    # mid-late band [1/2, 3/4]. They are shuffled so that no book always gets the same band. The
    # earliest target goes first, so each book is positioned against spheres that already reflect
    # the earlier moves.
    targets = [rng.uniform(0.0, 1 / 3), rng.uniform(1 / 3, 2 / 3), rng.uniform(1 / 2, 3 / 4)]
    rng.shuffle(targets)
    for book_loc, target in sorted(zip(book_locs, targets), key=lambda pair: pair[1]):
        # A location reachable in this state needs neither the book nor anything that the book
        # unlocks, such as the way to Chaos, so moving the book there can't put it behind itself.
        without_book = _state_without(multiworld, book_loc)
        position = _run_position(multiworld, player)
        candidates = sorted(
            (loc for loc in position if loc is not book_loc and loc.item is not None
             and loc.item.player == player and not loc.item.advancement and not loc.locked
             and _swap_allowed(book_loc, loc) and loc.can_reach(without_book)),
            key=lambda loc: (abs(position[loc] - target), loc.name))
        for dest in candidates[:BOOK_REFINE_TRIES]:
            if _swap_if_safe(multiworld, book_loc, dest):
                break


# --- pass 1: spread the Ancient Books (pre_fill) ----------------------------------------------

def spread_ancient_books(world: "CVAOSWorld") -> None:
    """
    Place the held-back Ancient Books in early, middle and late bands of map depth, before the main
    fill.

    The bands split this player's empty, non-excluded pickup locations, sorted by map depth, into
    three near-equal parts. The books are shuffled and each is aimed at a different band, with the
    long-tailed jitter, so a book sometimes lands in another band. A book goes only where it is
    reachable without any of the books. One that fits in no band goes into the multiworld pool for
    the main fill.

    Map depth stands in for run order because the spheres don't exist yet. ``refine_ancient_books``
    moves the books toward their final targets after the main fill.
    """
    multiworld = world.multiworld
    player = world.player
    params = PARAMS_BY_KEY[world.options.item_smoothing.current_key]
    rng = world.random

    books = [item for item in world.get_pre_fill_items() if item.name in ANCIENT_BOOK_NAMES]
    if not books:
        return

    depth = region_depth(multiworld, player)
    candidates = [loc for loc in multiworld.get_locations(player)
                  if loc.address in pickup_location_ids and loc.item is None and not loc.locked
                  and loc.progress_type != LocationProgressType.EXCLUDED
                  and loc.parent_region is not None and loc.parent_region.name in depth]
    if not candidates:
        return
    candidates.sort(key=lambda loc: (depth[loc.parent_region.name], loc.name))
    bands = _partition(candidates, 3)

    # Place each book only where it's reachable without any book, so a book is never locked behind its
    # own gate. Items already placed count only once their locations are reachable that way, so a
    # book also can't land behind an item that is itself behind the books, such as a plando'd one.
    without_books = _all_state_except(multiworld, books)

    rng.shuffle(books)  # so Book 1/2/3 aren't deterministically early/mid/late
    world._spread_books = []
    for index, book in enumerate(books):
        if _place_in_band(book, bands, index, params, rng, without_books):
            world._spread_books.append(book)
        else:
            _add_to_pool(world, book)  # safety: no candidate fit -> let the main fill place it


def _partition(items: List[Location], parts: int) -> List[List[Location]]:
    """Split a list into ``parts`` contiguous, near-equal chunks."""
    length = len(items)
    return [items[i * length // parts:(i + 1) * length // parts] for i in range(parts)]


def _place_in_band(book: Item, bands: List[List[Location]], index: int,
                   params: SmoothingParams, rng, state: CollectionState) -> bool:
    """
    Place ``book`` in a band chosen near band ``index`` (0 early, 1 middle, 2 late), and return
    whether it was placed.

    The target is the middle of band ``index`` plus the long-tailed jitter (or, rarely, a uniform
    wildcard), so it sometimes falls in another band. The band that the target falls in is tried
    first, then the other two. Within a band, the book goes to a random free location that is
    reachable in ``state`` and accepts it, and that location is locked.
    """
    target = jittered((index + 0.5) / 3.0, params.sigma_base, params, rng)
    clamped = min(0.999999, max(0.0, target))
    band_index = int(clamped * 3)
    order = [band_index] + [other for other in range(3) if other != band_index]
    for which in order:
        band = list(bands[which])
        rng.shuffle(band)
        for loc in band:
            if loc.item is None and loc.can_fill(state, book, True):
                loc.item = book
                book.location = loc
                loc.locked = True
                return True
    return False


# --- pass 2: smooth the remaining non-progression items (stage_post_fill) ---------------------

def smooth_placed_items(multiworld: MultiWorld, game: str) -> None:
    """
    Reorder the placed items of every ``game`` world with smoothing on, after the main fill.

    A world with Wide Item Smoothing on is skipped, because ``wide_smoothing`` reorders its items
    instead. For each other world, ``smoothable_locations_in_order`` gives the locations whose
    items may move, earliest first, and ``_assign_targets`` gives each of their items a target.
    The items are then sorted by target and reassigned in that order, so the earliest location
    gets the lowest-target item that it accepts. The spheres are computed once and shared by every
    world.

    The moved items are non-progression except for the floor and ceiling breakers. If the new
    arrangement fails the accessibility check, the breakers go back to the locations the main fill
    gave them, and the other items keep their new locations.
    """
    worlds = [world for world in multiworld.get_game_worlds(game)
              if world.options.item_smoothing.current_key != "off"
              and not world.options.wide_item_smoothing]
    if not worlds:
        return
    spheres = list(multiworld.get_spheres())
    for world in worlds:
        params = PARAMS_BY_KEY[world.options.item_smoothing.current_key]
        depth = region_depth(multiworld, world.player)
        by_map_distance = world.options.item_smoothing_order.current_key == "map_distance"
        ordered = smoothable_locations_in_order(multiworld, world.player, spheres, depth,
                                                by_map_distance=by_map_distance)
        if not ordered:
            continue
        # The ceiling and floor breakers are reordered here like other equipment, since
        # smoothable_locations_in_order includes them. They are advancement, and _reassign doesn't
        # check reachability, so first record where the main fill put each one. A copy that an
        # enemy check added is only useful, so logic never counts on it and it isn't recorded. The
        # homes are pairs rather than a dict, because copies of an item compare equal.
        breaker_homes = [(loc.item, loc) for loc in ordered
                         if loc.item.advancement and loc.item.name in logic_only_progression_names]
        items = [loc.item for loc in ordered]
        targets = _assign_targets(items, params, world.random)
        by_target = sorted(range(len(items)), key=lambda i: targets[i])
        items = [items[i] for i in by_target]  # ascending t == least desirable first
        _reassign(ordered, items, multiworld.state)
        # If the new arrangement put a floor or ceiling gate out of reach, put the breakers back
        # where the main fill had them. The non-advancement items keep their new locations, since
        # they don't affect logic.
        if breaker_homes and not accessible(multiworld):
            restore_breakers(breaker_homes)


def restore_breakers(breaker_homes: List[Tuple[Item, Location]]) -> None:
    """Swap each breaker in ``breaker_homes`` back to the location paired with it.

    Only the breakers go back. The item now at each home moves to where its breaker was, and every
    other item keeps its location.
    """
    for breaker, home in breaker_homes:
        current = breaker.location
        if current is home:
            continue
        displaced = home.item
        current.item, home.item = displaced, breaker
        breaker.location, displaced.location = home, current


def _reassign(ordered_locs: List[Location], sorted_items: List[Item], state: CollectionState) -> None:
    """
    Fill ``ordered_locs`` in order, giving each the first remaining item in ``sorted_items`` that
    it accepts (see ``_pop_fillable``).
    """
    pool = list(sorted_items)
    for loc in ordered_locs:
        new_item = _pop_fillable(loc, pool, state)
        loc.item = new_item
        new_item.location = loc


def _pop_fillable(loc: Location, pool: List[Item], state: CollectionState) -> Item:
    """
    Remove and return the first item in ``pool`` that ``loc`` accepts under its item rule and
    excluded setting, or the first item if it accepts none.
    """
    for i, item in enumerate(pool):
        if loc.can_fill(state, item, False):
            return pool.pop(i)
    return pool.pop(0)  # nothing fits cleanly; give up and assign the next
