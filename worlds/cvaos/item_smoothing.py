"""
Item-placement smoothing..

Two logic-safe passes, both gated on the ``item_smoothing`` option:

* Ancient Books (``spread_ancient_books`` in ``World.pre_fill`` + ``refine_ancient_books`` in
     ``World.stage_post_fill``): spread across early/mid/late thirds -- roughed in by map depth
     before the fill, then refined onto the real sphere-thirds afterward via logic-verified swaps.
* ``smooth_placed_items`` (``World.stage_post_fill``): non-progression items are re-assigned among
     the locations that already hold them, ordered earliest-first along the axis chosen by the
     ``item_smoothing_order`` option -- logical spheres (default, seed-specific) or structural map
     distance (seed-independent) -- so low-desirability items land early and high-desirability items
     land late, with long-tailed per-item jitter (Student-t) plus a wildcard, so a lucky early find
     is rare-but-possible.

Re-assigning only non-progression items among their own already-reachable locations never changes
   reachability, so the post-fill pass cannot break logic.
"""

from __future__ import annotations

import math
import re
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, TYPE_CHECKING

from BaseClasses import CollectionState, Item, Location, LocationProgressType, MultiWorld

from .data import item_balancing as desirability_data
from .data import item_info
from .items import logic_only_progression_names

if TYPE_CHECKING:
    from . import CVAOSWorld


# tuning knobs

@dataclass(frozen=True)
class SmoothingParams:
    nu: float          # Student-t d.o.f. (tail weight): low -> heavy tails/extreme jumps that pile at
                       # the front; higher -> lighter tails, so early jumps spread across the middle.
    sigma_base: float  # baseline jitter width (fraction of the run) every item gets
    k: float           # extra jitter width per unit of rater disagreement
    eps: float         # wildcard probability: ignore the target and place uniformly at random
    gold_lo: float     # money band (stratified across all copies), low edge
    gold_hi: float     # money band, high edge


# Keyed by the option's current_key.
# Jitter is applied in position space (see _assign_targets), so nu/sigma control how far through the
# run an item drifts. nu is kept moderate so anomalous early finds land across the early game rather
# than always cratering to the very first pickup.
PARAMS_BY_KEY: Dict[str, SmoothingParams] = {
    "loose":  SmoothingParams(nu=2.0, sigma_base=0.08, k=1.5, eps=0.05, gold_lo=0.0, gold_hi=0.55),
    "strict": SmoothingParams(nu=4.0, sigma_base=0.03, k=0.8, eps=0.02, gold_lo=0.0, gold_hi=0.55),
}

ANCIENT_BOOK_NAMES = ("Ancient Book 1", "Ancient Book 2", "Ancient Book 3")

# Soft placement bias for the three gating mobility souls -- a *separate* axis from desirability
# (which rates all three highly). The lateness targets here are 0 = earliest reachable, 1 = latest.
BIASED_SOULS = ("Malphas", "Hippogryph", "Giant Bat")

# Black Panther (dash) is biased early on its own toggle (black_panther_bias), independent of the three
# above. The window/chance are fixed (not exposed): cranking the chance saturates -- the early-placement
# ceiling is structural for a gating soul under logic-safe placement -- so a single on/off is enough.
BLACK_PANTHER = "Black Panther"
BLACK_PANTHER_EARLY_WINDOW = 0.5   # when biased, aim into the first 50% of the run (sphere ordering)...
BLACK_PANTHER_EARLY_CHANCE = 1.0   # ...in ~80% of seeds (100% saturates); the rest get normal placement

# Each soul's "late" target (lateness center, jitter spread) for when it is NOT the run's early pick.
# Kept genuinely late so only the *designated* soul lands early -- otherwise the joint guarantee is
# over-satisfied and every soul drifts early.
SOUL_LATE_TARGETS = {
    "Malphas": (0.60, 0.16),     # double jump: only early when it's the designated pick
    "Hippogryph": (0.86, 0.13),  # generally late (pushed further right)
    "Giant Bat": (0.92, 0.10),   # latest
}
# Each run, with EARLY_GUARANTEE probability, one of the three is pulled early so you have basic
# mobility in the first third; the other ~7% of runs leave all three on their late targets. Which
# soul is the early pick is weighted toward double jump.
EARLY_GUARANTEE = 0.93
EARLY_PICK_WEIGHTS = {"Malphas": 0.72, "Hippogryph": 0.18, "Giant Bat": 0.1}
EARLY_BAND = 0.33            # designated-early soul lands uniformly across the first third (lateness),
                             # rather than pinned to the literal first pickup
SOUL_NU = 3.0                # moderate tail weight for the late soul targets

# Progression souls that gate movement -- used to define "ungated" (reachable with base abilities
# only). Whether a location needs one of these is a far better "late in the run" signal than map
# distance for games without enemy rando, so it drives the soul-placement lateness metric below.
MOVEMENT_SOULS = frozenset({
    "Flying Armor", "Giant Bat", "Black Panther", "Undine", "Skula",
    "Grave Keeper", "Skeleton Blaze", "Malphas", "Hippogryph", "Galamoth",
})

ORIGIN_REGION = "Menu"
_UNREACHED_DEPTH = 10 ** 9  # sort graph-disconnected regions last

_SUFFIX = re.compile(r"\s\([^)]*\)$")  # " (A)" / " (foo)" disambiguation suffix on display names
_GOLD = re.compile(r"^\d+ Gold$")


# item identity

def _base_name(display_name: str) -> str:
    """
    Strip any disambiguation suffix to get the canonical item name.
    """
    return _SUFFIX.sub("", display_name)


def _category(base_name: str) -> str:
    """
    Get the category of an item based on its base name.
    """
    info = item_info.by_name.get(base_name)
    if info is not None:
        return info.item_category
    if _GOLD.match(base_name):
        return "money"
    return "unknown"


# target-position model

def _student_t(nu: float, rng) -> float:
    """
    Standard Student-t draw. nu -> inf approaches Gaussian; nu = 1 is Cauchy (longest tails).
    """
    chi2 = rng.gammavariate(nu / 2.0, 2.0)  # chi-squared with nu d.o.f.
    if chi2 <= 0.0:  # gammavariate can underflow to 0.0 for small shape; avoid a divide-by-zero
        return 0.0
    return rng.gauss(0.0, 1.0) / math.sqrt(chi2 / nu)


def _jittered(base: float, scale: float, params: SmoothingParams, rng) -> float:
    """
    A target near ``base`` with long-tailed jitter, or a uniform wildcard with prob ``eps``.
    """
    if rng.random() < params.eps:
        return rng.random()
    return base + scale * _student_t(params.nu, rng)


def _assign_targets(items: List[Item], params: SmoothingParams, rng) -> Dict[Item, float]:
    """A placement target (higher = later) per item, jittered in *position* space.

    Two steps, so the long tail keeps a middle ground:

    1. Put every item on a base in desirability space: money stratified across the early-to-mid band,
       ranked items at their desirability, unranked at a uniform prior.
    2. **Rank-transform** those bases into uniform positions in [0, 1], then add the long-tailed
       jitter to the *position*.

    Why the rank-transform: desirability is densely packed at the low end and sparse at the top, so
       jittering in desirability space and then placing by rank is nonlinear -- a top item has to cross
       the whole dense pack, so a small jitter barely moves it and a big one overshoots to the very
       front, with little in between.
    Jittering the (uniform) position instead makes displacement linear:
       a moderate draw moves an item a moderate fraction of the run. Tail weight (how often a draw is
       extreme) is set by ``nu``. A wildcard (prob ``eps``) ignores the target.
    Not clamped: the placement pass sorts by target, so an extreme draw just yields an extreme rank.
    """
    base: Dict[Item, float] = {}
    scale: Dict[Item, float] = {}

    # Money is stratified by copy across the early-to-mid band below, NOT ranked: the money rows in
    # desirability.csv are intentionally ignored here -- gold quantity stratification, not rater
    # desirability, sets where gold lands.
    golds = [it for it in items if _category(_base_name(it.name)) == "money"]
    rng.shuffle(golds)
    span = params.gold_hi - params.gold_lo
    count = len(golds)
    for index, item in enumerate(golds):
        lo = params.gold_lo + (index / count) * span
        hi = params.gold_lo + ((index + 1) / count) * span
        base[item] = rng.uniform(lo, hi)
        scale[item] = params.sigma_base

    for item in items:
        if item in base:
            continue
        info = desirability_data.by_name.get(_base_name(item.name))
        if info is not None:
            base[item] = info.desirability
            scale[item] = params.sigma_base + params.k * info.disagreement
        else:
            base[item] = rng.random()  # unranked: uniform prior over the whole run
            scale[item] = params.sigma_base

    # Rank-transform the bases into evenly spaced positions, preserving order.
    order = sorted(items, key=lambda it: base[it])
    denom = max(1, len(order) - 1)
    position = {item: i / denom for i, item in enumerate(order)}

    targets: Dict[Item, float] = {}
    for item in items:
        if rng.random() < params.eps:
            targets[item] = rng.random()  # wildcard: ignore the target, place anywhere
        else:
            targets[item] = position[item] + scale[item] * _student_t(params.nu, rng)
    return targets


# shared progress ordering

def _region_depth(multiworld: MultiWorld, player: int) -> Dict[str, int]:
    """
    BFS map-distance of each region from the origin, ignoring access rules (structural depth).
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
    This player's own non-progression items sitting in this player's own locations, ordered
       earliest-first. Shared by the smoothing pass and the tests so they agree on 'progress order'.

    The ordering axis is chosen by the ``item_smoothing_order`` option (see options.py):

    * default (``by_map_distance=False``): by ``(sphere, map depth, name)`` -- logical reachability
         order, measured against *this* seed's real spheres (seed-specific).
    * ``by_map_distance=True``: by ``(map depth, name)`` only -- structural distance from the start
         in the unrandomized map, ignoring spheres (seed-independent).

    Either way the candidate set is drawn from the spheres, so only reachable locations are smoothed
       and the two axes differ only in order, not in which locations they touch.

    Scoped to on-world locations (``loc.player == player``) on purpose: a cvaos item sent off-world
       isn't gear its recipient can use, so ordering it in a foreign world is meaningless -- and it
       keeps the order a deterministic total order (own location names are unique) and respects
       local/non_local placement for free.
    Excluded locations are left alone: they may hold only filler, so moving a ``useful`` item into one
       would be illegal (and they hold junk regardless)."""
    # get_spheres yields the reachable spheres, then an empty sentinel, then a set of all *unreachable*
    # filled locations. Stop at the sentinel so genuinely-unreachable spots are never smoothed -- keeps
    # the "only reachable locations are smoothed" contract true under accessibility=items/minimal too.
    sphere_of: Dict[Location, int] = {}
    for index, sphere in enumerate(spheres):
        if not sphere:
            break
        for loc in sphere:
            sphere_of[loc] = index
    candidates = [loc for loc in sphere_of
                  if loc.item is not None
                  and loc.player == player
                  and loc.item.player == player
                  and loc.parent_region is not None
                  and not loc.locked
                  and loc.progress_type != LocationProgressType.EXCLUDED
                  and (not loc.item.advancement
                       or loc.item.name in logic_only_progression_names)]
    if by_map_distance:
        candidates.sort(key=lambda loc: (depth.get(loc.parent_region.name, _UNREACHED_DEPTH), loc.name))
    else:
        candidates.sort(key=lambda loc: (sphere_of[loc],
                                         depth.get(loc.parent_region.name, _UNREACHED_DEPTH), loc.name))
    return candidates


# --- pass 0: bias the gating mobility souls (pre_fill) ----------------------------------------

def _lateness_ranking(multiworld: MultiWorld, player: int, depth: Dict[str, int]) -> Dict[Location, float]:
    """
    Per-location "lateness" in [0, 1]: a logic-aware proxy for how late in the run a location is
       reached -- far better than raw map distance, which can put deep-but-ungated rooms early.

    The signal is a placement-independent **ability tier**: collect this player's movement souls one
       at a time, most-impactful first, and record at which step each location first becomes reachable.
       Tier 0 = reachable with no soul (early-sphere); a location needing many souls lands at a high
       tier (late).
    Map depth only breaks ties within a tier. (`gain`/`solo` sweeps are O(#souls) and
       run once per world during pre_fill.)
    """
    world = multiworld.worlds[player]
    souls = [item for item in multiworld.itempool if item.player == player and item.name in MOVEMENT_SOULS]
    locs = [loc for loc in multiworld.get_locations(player)
            if loc.address is not None and loc.parent_region is not None
            and loc.parent_region.name in depth]

    base = CollectionState(multiworld)
    for item in multiworld.itempool:
        if not (item.player == player and item.name in MOVEMENT_SOULS):
            multiworld.worlds[item.player].collect(base, item)
    base.sweep_for_advancements()

    def solo_reach(soul: Item) -> int:
        trial = base.copy()
        world.collect(trial, soul)
        trial.sweep_for_advancements()
        return sum(1 for loc in locs if loc.can_reach(trial))

    # Most-unlocking first; name breaks ties so the order is a total order (independent of itempool
    # insertion order), matching the deterministic-sort discipline used everywhere else in this module.
    souls.sort(key=lambda soul: (-solo_reach(soul), soul.name))

    state = base
    tier: Dict[Location, int] = {}
    reached = set()
    step = 0
    for soul in [None, *souls]:  # step 0 = no souls, then one soul per step
        if soul is not None:
            step += 1
            world.collect(state, soul)
            state.sweep_for_advancements()
        for loc in locs:
            if loc not in reached and loc.can_reach(state):
                reached.add(loc)
                tier[loc] = step
    for loc in locs:
        tier.setdefault(loc, step + 1)  # never reachable -> latest

    locs.sort(key=lambda loc: (tier[loc], depth[loc.parent_region.name], loc.name))
    denom = max(1, len(locs) - 1)
    return {loc: i / denom for i, loc in enumerate(locs)}


def bias_progression_souls(world: "CVAOSWorld") -> None:
    """
    Aim the gating mobility souls at soft *lateness* targets, logic-safely.
    These are the three early/late souls (Malphas, Hippogryph, Giant Bat) and -- 
       on its own probability/window knobs -- Black Panther.

    They gate regions, so they can't ride the post-fill smoothing; instead we place them (before the
       main fill) at the reachable location whose logic-aware lateness (see ``_lateness_ranking``) is
       nearest each one's target, then ``refine_soul_positions`` nudges them to the target by real sphere
       ordering post-fill.
       
    Items are placed earliest-target first, recomputing reachability after each, so
       a later one may *use* an already-placed earlier one to reach gated locations while never depending
       on one placed after it (topological: none behind itself). ``fill_restrictive`` refuses to place
       where unreachable; if nothing fits, the item is handed back to the normal fill, so the bias can
       never make a seed unbeatable.

    Gating: the three souls follow ``early_mobility_souls`` (``off`` skips them; ``guarantee_early``
       forces one early every run; ``bias_early`` does so ~``EARLY_GUARANTEE`` of runs). Black Panther is
       independent, gated by the ``black_panther_bias`` toggle: when on, ~``BLACK_PANTHER_EARLY_CHANCE`` of
       runs aim it into the first ``BLACK_PANTHER_EARLY_WINDOW`` of the run (same sphere-ordering axis).
    """
    from Fill import fill_restrictive

    multiworld = world.multiworld
    player = world.player
    rng = world.random

    # Candidate items: the three gating souls and, separately, Black Panther. Each may get a lateness
    # target in [0, 1] (0 = earliest by sphere ordering, 1 = latest); all targeted ones are then placed.
    found: Dict[str, Item] = {}
    for item in multiworld.itempool:
        if (item.player == player and item.name not in found
                and (item.name in BIASED_SOULS or item.name == BLACK_PANTHER)):
            found[item.name] = item

    targets: Dict[Item, float] = {}

    # The three mobility souls, gated by early_mobility_souls ("off" skips them entirely). Designate
    # the run's early-mobility soul (or none), then give each a lateness target: the designated one
    # aims very early, the rest at their late centers.
    mode = world.options.early_mobility_souls.current_key
    present = [found[name] for name in BIASED_SOULS if name in found]
    if mode != "off" and present:
        guarantee = 1.0 if mode == "guarantee_early" else EARLY_GUARANTEE
        designated = None
        if rng.random() < guarantee:
            names = [item.name for item in present]
            designated = rng.choices(names, weights=[EARLY_PICK_WEIGHTS[n] for n in names])[0]

        def soul_target(name: str) -> float:
            if name == designated:
                return rng.uniform(0.0, EARLY_BAND)  # spread across the early region, not pinned to #1
            center, spread = SOUL_LATE_TARGETS[name]
            return min(1.0, max(0.0, center + spread * _student_t(SOUL_NU, rng)))

        for item in present:
            targets[item] = soul_target(item.name)

    # Black Panther: independent on/off toggle (black_panther_bias) -- when on, ~BLACK_PANTHER_EARLY_CHANCE
    # of runs aim it into the first BLACK_PANTHER_EARLY_WINDOW of the run (same seed/sphere-ordering axis
    # as the souls above).
    panther = found.get(BLACK_PANTHER)
    if panther is not None and world.options.black_panther_bias.value:
        if rng.random() < BLACK_PANTHER_EARLY_CHANCE:
            targets[panther] = rng.uniform(0.0, BLACK_PANTHER_EARLY_WINDOW)

    if not targets:
        return

    depth = _region_depth(multiworld, player)
    lateness = _lateness_ranking(multiworld, player, depth)
    # Stored for the post-fill refine pass, which moves each item to this target by *real* run
    # position (sphere ordering); these values double as the rough pre-fill lateness used just below.
    world._soul_targets = {item.name: target for item, target in targets.items()}

    for item in targets:
        multiworld.itempool.remove(item)

    # Place earliest-target first, recomputing the state each time so a later item can reach gated
    # locations behind the already-placed earlier ones (topological order: none behind itself).
    for item in sorted(targets, key=lambda it: targets[it]):
        base_state = multiworld.get_all_state()
        candidates = [loc for loc in lateness if loc.item is None and not loc.locked]
        candidates.sort(key=lambda loc: (abs(lateness[loc] - targets[item]), loc.name))
        # Single-item pool on purpose: fill_restrictive's swap branch (the only consumer of the shared
        # multiworld.random here) needs >=2 placements to run, so a 1-item pool keeps soul biasing free
        # of cross-player RNG-ordering -- multiworld-safe. Don't batch multiple souls into one call.
        pool = [item]
        fill_restrictive(multiworld, base_state, candidates, pool,
                         single_player_placement=True, lock=True, allow_partial=True)
        if pool:  # nothing reachable fit its target -> let the normal fill place it
            multiworld.itempool.append(item)


# --- pass 0b: refine soul positions against the real spheres (post-fill) ----------------------

SOUL_REFINE_TRIES = 8  # post-fill swap candidates to try per soul before leaving it where it is


def _run_position(multiworld: MultiWorld, player: int, *,
                  by_map_distance: bool = False) -> Dict[Location, float]:
    """
    Fraction through this player's run (0 = first reached, 1 = last). The ordering axis matches the
       ``item_smoothing_order`` option, same as the equipment smoothing:

    * default (``by_map_distance=False``): by global sphere then map depth -- the *real* post-fill,
       seed-specific position (what the distribution plots show); needs spheres, so only available here.
    * ``by_map_distance=True``: by structural map depth only (seed-independent), ignoring spheres.
    """
    depth = _region_depth(multiworld, player)
    locs = [loc for loc in multiworld.get_locations(player)
            if loc.address is not None and loc.item is not None and loc.parent_region is not None]
    if by_map_distance:
        locs.sort(key=lambda loc: (depth.get(loc.parent_region.name, 10**9), loc.name))
    else:
        spheres = list(multiworld.get_spheres())
        sphere_of = {loc: i for i, sphere in enumerate(spheres) for loc in sphere}
        locs.sort(key=lambda loc: (sphere_of.get(loc, 10**9),
                                   depth.get(loc.parent_region.name, 10**9), loc.name))
    denom = max(1, len(locs) - 1)
    return {loc: i / denom for i, loc in enumerate(locs)}


def _accessible(multiworld: MultiWorld) -> bool:
    """
    Bool wrapper for fulfills_accessibility, which *raises* FillError on a dead end under
       ``__debug__`` instead of returning False. It uses a fresh state, so it never mutates placement.
    """
    from Fill import FillError
    try:
        return bool(multiworld.fulfills_accessibility())
    except FillError:
        return False


def _swap_if_safe(multiworld: MultiWorld, soul_loc: Location, dest: Location) -> bool:
    """
    Swap the items at ``soul_loc`` and ``dest``, keeping it only if the *whole* multiworld still
       fulfills accessibility (so it is safe in any game combination). On success the soul's new home is
       locked and the freed spot is unlocked for the equipment smoothing.
    """
    soul, other = soul_loc.item, dest.item
    soul_loc.item, dest.item = other, soul
    soul.location, other.location = dest, soul_loc
    if _accessible(multiworld):
        soul_loc.locked, dest.locked = False, True
        return True
    soul_loc.item, dest.item = soul, other  # revert
    soul.location, other.location = soul_loc, dest
    return False


def refine_soul_positions(world: "CVAOSWorld") -> None:
    """
    Move each biased soul to its target run-position (chosen in pre_fill) using the real post-fill
       spheres, via logic-verified swaps with a non-progression item.
    Candidates are pre-filtered to
       locations reachable *without* this soul (so it is never placed behind itself), and each swap is
       kept only if the multiworld still fulfills accessibility -- so it can never make a seed (in any
       game combination) unbeatable. A soul that can't be moved safely stays where pre_fill left it.
    Runs before the equipment smoothing, which recomputes spheres afterward."""
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
        soul = soul_loc.item
        # State reachable with every placed item except this soul -> any location reachable here does
        # not need the soul, so moving it there can't strand the soul behind itself.
        without_soul = CollectionState(multiworld)
        for loc in multiworld.get_filled_locations():
            if loc.item is not soul:
                multiworld.worlds[loc.item.player].collect(without_soul, loc.item)
        without_soul.sweep_for_advancements()

        position = _run_position(multiworld, player)
        target = targets[name]
        candidates = sorted(
            (loc for loc in position if loc is not soul_loc and loc.item is not None
             and loc.item.player == player and not loc.item.advancement and not loc.locked
             and loc.can_reach(without_soul)),
            key=lambda loc: (abs(position[loc] - target), loc.name))
        for dest in candidates[:SOUL_REFINE_TRIES]:
            if _swap_if_safe(multiworld, soul_loc, dest):
                break


BOOK_REFINE_TRIES = 12  # post-fill swap candidates to try per book before leaving it where pre_fill put it


def refine_ancient_books(world: "CVAOSWorld") -> None:
    """
    Spread the three Ancient Books across early / middle / mid-late targets of the *real* run (sphere
       ordering), post-fill, via logic-verified swaps with a non-progression item.
    Mirrors ``refine_soul_positions``: each book is moved only to a location reachable *without* that
       book (so it is never placed behind its own gate), and each swap is kept only if the whole
       multiworld still fulfills accessibility. So it stays correct and safe even once the books are
       required progression -- e.g. needed to reach Graham for the good ending, a real access rule in
       item_requirements -- exactly as the gating mobility souls are handled today. A book that can't
       be moved safely stays where ``spread_ancient_books`` left it.
    Spheres only exist post-fill, so ``spread_ancient_books`` can only rough the books in pre-fill;
       this finishes the job. Runs before the equipment smoothing.
    """
    multiworld = world.multiworld
    player = world.player
    rng = world.random
    book_locs = [loc for loc in multiworld.get_locations(player)
                 if loc.item is not None and loc.item.player == player
                 and loc.item.name in ANCIENT_BOOK_NAMES]
    if not book_locs:
        return

    # Targets: one book somewhere in the early third [0, 1/3], one in the middle third [1/3, 2/3], and
    # one in the mid-late band [1/2, 3/4]. Shuffled so a given book isn't deterministically assigned;
    # earliest target first so a book is positioned against spheres that already reflect earlier ones.
    targets = [rng.uniform(0.0, 1 / 3), rng.uniform(1 / 3, 2 / 3), rng.uniform(1 / 2, 3 / 4)]
    rng.shuffle(targets)
    for book_loc, target in sorted(zip(book_locs, targets), key=lambda pair: pair[1]):
        book = book_loc.item
        # State reachable with every placed item except this book -> any location reachable here does
        # not need the book, so moving it there can't strand the book (or anything gated behind it,
        # e.g. Graham once the books are required) behind itself.
        without_book = CollectionState(multiworld)
        for loc in multiworld.get_filled_locations():
            if loc.item is not book:
                multiworld.worlds[loc.item.player].collect(without_book, loc.item)
        without_book.sweep_for_advancements()

        position = _run_position(multiworld, player)
        candidates = sorted(
            (loc for loc in position if loc is not book_loc and loc.item is not None
             and loc.item.player == player and not loc.item.advancement and not loc.locked
             and loc.can_reach(without_book)),
            key=lambda loc: (abs(position[loc] - target), loc.name))
        for dest in candidates[:BOOK_REFINE_TRIES]:
            if _swap_if_safe(multiworld, book_loc, dest):
                break


# Ceiling/floor breakers are no longer refined by a separate swap pass: they're advancement (so fill
# keeps them reachable) but `smoothable_locations_in_order` lets them into the normal equipment
# smoothing, where they spread by desirability with the other gear. `smooth_placed_items` then verifies
# accessibility and reverts only the breakers if the distribution ever strands a floor/ceiling gate.


# --- pass 1: spread the Ancient Books (pre_fill) ----------------------------------------------

def spread_ancient_books(world: "CVAOSWorld") -> None:
    multiworld = world.multiworld
    player = world.player
    params = PARAMS_BY_KEY[world.options.item_smoothing.current_key]
    rng = world.random

    books = [item for item in multiworld.itempool
             if item.player == player and item.name in ANCIENT_BOOK_NAMES]
    if not books:
        return

    depth = _region_depth(multiworld, player)
    candidates = [loc for loc in multiworld.get_locations(player)
                  if loc.address is not None and loc.item is None and not loc.locked
                  and loc.parent_region is not None and loc.parent_region.name in depth]
    if not candidates:
        return
    candidates.sort(key=lambda loc: (depth[loc.parent_region.name], loc.name))
    bands = _partition(candidates, 3)

    # Place each book only where it's reachable WITHOUT any book, so a book is never locked behind its
    # own gate. A no-op while the books gate nothing, but required once they become real progression
    # (e.g. needed to reach Graham for the good ending) -- the same guard the gating souls rely on.
    without_books = CollectionState(multiworld)
    for item in multiworld.itempool:
        if not (item.player == player and item.name in ANCIENT_BOOK_NAMES):
            multiworld.worlds[item.player].collect(without_books, item)
    for loc in multiworld.get_filled_locations():
        multiworld.worlds[loc.item.player].collect(without_books, loc.item)
    without_books.sweep_for_advancements()

    rng.shuffle(books)  # so Book 1/2/3 aren't deterministically early/mid/late
    for index, book in enumerate(books):
        multiworld.itempool.remove(book)
        if not _place_in_band(book, bands, index, params, rng, without_books):
            multiworld.itempool.append(book)  # safety: no candidate fit -> let the main fill place it


def _partition(items: List[Location], parts: int) -> List[List[Location]]:
    """Split a list into ``parts`` contiguous, near-equal chunks."""
    length = len(items)
    return [items[i * length // parts:(i + 1) * length // parts] for i in range(parts)]


def _place_in_band(book: Item, bands: List[List[Location]], index: int,
                   params: SmoothingParams, rng, state: CollectionState) -> bool:
    """
    Place ``book`` in its target band (early/mid/late by ``index``), drifting via the long-tailed
       jitter and falling back to the other bands. Returns True if placed.
    """
    target = _jittered((index + 0.5) / 3.0, params.sigma_base, params, rng)
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
    worlds = [world for world in multiworld.get_game_worlds(game)
              if world.options.item_smoothing.current_key != "off"]
    if not worlds:
        return
    spheres = list(multiworld.get_spheres())
    for world in worlds:
        params = PARAMS_BY_KEY[world.options.item_smoothing.current_key]
        depth = _region_depth(multiworld, world.player)
        by_map_distance = world.options.item_smoothing_order.current_key == "map_distance"
        ordered = smoothable_locations_in_order(multiworld, world.player, spheres, depth,
                                                by_map_distance=by_map_distance)
        if not ordered:
            continue
        # Ceiling/floor breakers ride the normal distribution here (smoothable_locations_in_order lets
        # them in), so they spread by desirability like any other equipment. They're advancement and
        # the bulk reassign doesn't check reachability, so remember where fill put each one first.
        breaker_homes = {loc.item: loc for loc in ordered
                         if loc.item.name in logic_only_progression_names}
        items = [loc.item for loc in ordered]
        targets = _assign_targets(items, params, world.random)
        items.sort(key=lambda it: targets[it])  # ascending t == least desirable first
        _reassign(ordered, items, multiworld.state)
        # If riding the distribution stranded a floor/ceiling gate, put the breakers back where fill
        # had them (guaranteed reachable); the non-advancement items keep their smoothed spots, since
        # they don't affect logic. Verified to essentially never trigger, but keeps seeds beatable.
        if breaker_homes and not _accessible(multiworld):
            _restore_breakers(breaker_homes)


def _restore_breakers(breaker_homes: Dict[Item, Location]) -> None:
    """Swap each breaker back to its pre-smoothing (fill) location, undoing only the breaker moves."""
    for breaker, home in breaker_homes.items():
        current = breaker.location
        if current is home:
            continue
        displaced = home.item
        current.item, home.item = displaced, breaker
        breaker.location, displaced.location = home, current


def _reassign(ordered_locs: List[Location], sorted_items: List[Item], state: CollectionState) -> None:
    """
    Drop the lowest-``t`` fillable item into the earliest location, and so on up the spheres.
    """
    pool = list(sorted_items)
    for loc in ordered_locs:
        new_item = _pop_fillable(loc, pool, state)
        loc.item = new_item
        new_item.location = loc


def _pop_fillable(loc: Location, pool: List[Item], state: CollectionState) -> Item:
    """
    The earliest-``t`` item that may legally sit in ``loc`` (respects excluded/item rules).
    """
    for i, item in enumerate(pool):
        if loc.can_fill(state, item, False):
            return pool.pop(i)
    return pool.pop(0)  # nothing fits cleanly; give up and assign the next
