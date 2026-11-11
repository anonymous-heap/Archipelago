"""
Reachability spheres over the routing graph with pickup nodes.

A sphere is one round of item collection. Sphere 0 holds the pickups reachable from the starting
entrance with the starting abilities, which are none by default. Sphere N holds the pickups that
become reachable once the player has every ability granted in spheres 0 to N-1. The rounds stop
when a round reaches no new pickup.

Each pickup grants the abilities of its item in the unrandomized game, so the spheres describe
the vanilla item placement. An ability soul such as Malphas grants its ability, which is DJump
for Malphas. An item that breaks ceilings or floors, such as Balmung, grants Ceil or Floor,
because generation satisfies those bits with such an item.

The Kick and Tform souls come only from enemies, and the Chronomage soul has no pickup, so no
pickup grants Kick, Tform or Chronomage. Pass them in ``initial_have_mask`` to assume them.

Run it from the repository root to print the spheres from Soma's starting entrance:

    python -m worlds.cvaos.tools.routing.spheres
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Set, Tuple

from ...data import AbilityCombo, item_info_collection, pickup_info_collection
from .entrances import MaskUtils, RoutingQueries
from .entrances_to_items import PickupNodeId, RoutingGraph, default_graph

NodeId = str
ReqMask = int

START_ENTRANCE: NodeId = "000:003"
"""The entrance node where Soma starts, which generation connects to its Menu region."""


# ─────────────────────────────────────────────────────────────
# Item → ability mapping
# ─────────────────────────────────────────────────────────────

# The ability each soul grants, for the souls that are ground pickups. This matches the soul
# items in regions._ABILITY_TO_ITEM, which also lists the souls that no pickup holds.
_SOUL_NAME_TO_ABILITY: dict[str, AbilityCombo] = {
    "Flying Armor":    AbilityCombo.Glide,
    "Skeleton Blaze":  AbilityCombo.Slide,
    "Malphas":         AbilityCombo.DJump,
    "Hippogryph":      AbilityCombo.HJump,
    "Undine":          AbilityCombo.WWalk,
    "Skula":           AbilityCombo.Dive,
    "Black Panther":   AbilityCombo.Panth,
    "Giant Bat":       AbilityCombo.Bat,
    "Grave Keeper":    AbilityCombo.BDash,
    "Galamoth":        AbilityCombo.Galamoth,
}

# Ceil and Floor for each item that breaks ceilings or floors, as regions.py satisfies those bits.
_BREAKER_ABILITIES: dict[str, ReqMask] = {
    info.name: (int(AbilityCombo.Ceil) if info.breaks_ceilings else 0)
    | (int(AbilityCombo.Floor) if info.breaks_floors else 0)
    for info in item_info_collection
}


def ability_mask_for_item(item_name: str) -> ReqMask:
    """
    Return the abilities that holding ``item_name`` gives, as a mask.

    The mask holds the soul's ability for an ability soul, and Ceil or Floor for an item that
    breaks ceilings or floors. Black Panther is both, so it gives Panth, Ceil and Floor.
    """
    soul_ability = _SOUL_NAME_TO_ABILITY.get(item_name, AbilityCombo.None_)
    return int(soul_ability) | _BREAKER_ABILITIES.get(item_name, 0)


def build_pickup_to_ability_mask() -> dict[int, ReqMask]:
    """
    Return a mapping from each pickup number to the abilities its vanilla item gives.

    Pickups whose item gives no ability are left out.
    """
    result: dict[int, ReqMask] = {}
    for pickup in pickup_info_collection:
        mask = ability_mask_for_item(pickup.simple_name)
        if mask:
            result[pickup.pickup_number] = mask
    return result


# ─────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Sphere:
    index: int
    have_mask_before: ReqMask          # abilities when entering this sphere
    have_mask_after: ReqMask           # abilities after collecting this sphere's pickups
    newly_reachable_pickups: FrozenSet[int]
    abilities_gained: ReqMask          # ability bits that this sphere adds


@dataclass(frozen=True)
class SphereResult:
    spheres: Tuple[Sphere, ...]
    unreachable_pickups: FrozenSet[int]
    final_have_mask: ReqMask


# ─────────────────────────────────────────────────────────────
# Core algorithm
# ─────────────────────────────────────────────────────────────

def build_reachability_spheres(
    graph: RoutingGraph,
    start_entrance: NodeId,
    pickup_to_abilities: dict[int, ReqMask],
    *,
    initial_have_mask: ReqMask = 0,
) -> SphereResult:
    """
    Build the reachability spheres from ``start_entrance``.

    Each round searches the graph with the abilities held so far. The pickups that the search
    reaches for the first time form that round's sphere, and the abilities they grant are added
    before the next round. The rounds stop when a search reaches no new pickup.

    Args:
        graph: The extended graph with pickup nodes, as ``default_graph()`` returns.
        start_entrance: The node id to start each search from, such as ``"000:003"``.
        pickup_to_abilities: Maps a pickup number to the abilities that collecting it grants.
        initial_have_mask: The abilities held before sphere 0, which default to none.

    Returns:
        A SphereResult with one Sphere for each round that reached a new pickup, the pickups
        that no round reached, and the abilities held at the end.
    """
    all_pickup_numbers: FrozenSet[int] = frozenset(
        PickupNodeId.number(node_id)
        for node_id in graph.adj
        if PickupNodeId.is_pickup(node_id)
    )

    collected_pickups: Set[int] = set()
    have_mask: ReqMask = initial_have_mask
    spheres: list[Sphere] = []

    while True:
        reachable_nodes = RoutingQueries.reachable_entrance_nodes(graph, start_entrance, have_mask)

        reachable_pickups = {
            PickupNodeId.number(n) for n in reachable_nodes if PickupNodeId.is_pickup(n)
        }

        newly_reachable = reachable_pickups - collected_pickups
        if not newly_reachable:
            break

        new_ability_bits: ReqMask = 0
        for pnum in newly_reachable:
            new_ability_bits |= pickup_to_abilities.get(pnum, 0)

        spheres.append(Sphere(
            index=len(spheres),
            have_mask_before=have_mask,
            have_mask_after=have_mask | new_ability_bits,
            newly_reachable_pickups=frozenset(newly_reachable),
            abilities_gained=new_ability_bits & ~have_mask,
        ))

        collected_pickups |= newly_reachable
        have_mask |= new_ability_bits

    return SphereResult(
        spheres=tuple(spheres),
        unreachable_pickups=frozenset(all_pickup_numbers - collected_pickups),
        final_have_mask=have_mask,
    )


# ─────────────────────────────────────────────────────────────
# Display
# ─────────────────────────────────────────────────────────────

def print_sphere_result(
    result: SphereResult,
    pickup_to_abilities: dict[int, ReqMask],
    *,
    verbose: bool = True,
) -> None:
    total_reached = sum(len(s.newly_reachable_pickups) for s in result.spheres)
    print(f"=== Sphere Generation: {len(result.spheres)} sphere(s), {total_reached} pickup(s) reached ===\n")

    for sphere in result.spheres:
        count = len(sphere.newly_reachable_pickups)
        before_text = MaskUtils.format(sphere.have_mask_before)
        gained_text = MaskUtils.format(sphere.abilities_gained) if sphere.abilities_gained else "(none)"
        print(f"Sphere {sphere.index}:  {count:3d} pickup(s)  |  entering with: {before_text}  |  new abilities: {gained_text}")  # noqa: E501

        if verbose:
            ability_pickups = sorted(
                pnum for pnum in sphere.newly_reachable_pickups
                if pickup_to_abilities.get(pnum, 0)
            )
            other_pickups = sorted(
                pnum for pnum in sphere.newly_reachable_pickups
                if not pickup_to_abilities.get(pnum, 0)
            )

            for pnum in ability_pickups:
                ability_label = MaskUtils.format(pickup_to_abilities[pnum])
                print(f"    pickup {pnum:3d}  -> grants [{ability_label}]")
            if other_pickups:
                print(f"    + {len(other_pickups)} non-ability pickup(s): {other_pickups}")

    print(f"\nFinal abilities: {MaskUtils.format(result.final_have_mask)}")
    if result.unreachable_pickups:
        print(f"Unreachable ({len(result.unreachable_pickups)}): {sorted(result.unreachable_pickups)}")
    else:
        print("All pickups reachable.")


def main() -> None:
    """Print the reachability spheres from Soma's starting entrance."""
    pickup_to_abilities = build_pickup_to_ability_mask()
    result = build_reachability_spheres(default_graph(), START_ENTRANCE, pickup_to_abilities)
    print_sphere_result(result, pickup_to_abilities, verbose=True)


if __name__ == "__main__":
    main()
