"""
Regions and access rules for Castlevania: Aria of Sorrow.

Most regions are door nodes, one for each door of each room, named "Entrance: {room}:{neighbor}".
Each door crossing connects a node to the matching node of the neighboring room. Routing rows from
the requirement CSVs connect a room's door nodes to each other and to the room's pickup and enemy
regions, and each row's requirement masks become the access rule of its connection.

A few rules are written in code instead of the CSVs, such as the true-ending gate on the way to
the Chaotic Realm and the Forbidden Area barrier. The can_reach_* functions test region
reachability for the goals and for those rules.
"""
from __future__ import annotations

import functools
import operator
from typing import TYPE_CHECKING, Callable

from BaseClasses import Entrance, LocationProgressType, Region

from .data import (
    entrance_info_collection,
    pickup_info_collection,
    entrance_to_entrance_info_collection,
    entrance_to_pickup_region_info_collection,
    entrance_to_enemy_region_info_collection,
    by_enemy_name_for_enemy_regions,
    enemy_meta_by_number,
    by_enemy_number_for_enemy_regions,
    item_info_collection,
    AbilityCombo,
    transdoor_connection_collection,
)
from .enemy_checks import (ARENA_PLACEMENTS, EXCLUDED_TYPES, FALLBACK_SOULS, SPAWNS_AFTER_BOSS_IN,
                           defeat_location_name, enemy_check_types, placements_of)
from .locations import CVAOSLocation, location_name_to_id
from .options import (TECHNIQUES, ForbiddenAreaButton, hard_mode_pickups_included,
                      resolve_allowed_techniques)

if TYPE_CHECKING:
    from . import CVAOSWorld
    from BaseClasses import CollectionState

__all__ = [
    "create_regions",
    "can_reach_any_enemy",
    "can_reach_room",
    "LOGIC_ENEMY_TYPES",
]


# Enemy types whose souls satisfy the Tform (transformation) requirement bit. A Curly soul also
# works in-game, but logic leaves Curly out. That only makes logic stricter, because a missing
# source can never put a location in logic too early.
TFORM_ENEMY_TYPES: frozenset[str] = frozenset({"Devil", "Manticore"})

# Enemy types whose instances get enemy regions, so logic can test whether the player can reach a
# source of their soul. Only the types that logic reads are listed. Flame Demon and Succubus drop
# two of the true-ending souls, while the third, Giant Bat, is a ground pickup that logic checks
# with state.has. Devil and Manticore drop transformation souls (Tform). Graham is not listed,
# because logic treats reaching his room, 904, as beating him (see _chaotic_realm_gate and the
# goal in __init__).
LOGIC_ENEMY_TYPES: frozenset[str] = frozenset({"Flame Demon", "Succubus"}) | TFORM_ENEMY_TYPES

# Instances of those types that never respawn once defeated, so logic cannot expect the player to
# farm their soul. The room-106 Manticore (#132) is a one-time boss: EnemyManticoreCreate deletes it
# once its boss-defeated bit is set, which leaves a single roll for its soul.
ONE_TIME_ENEMY_NUMBERS: frozenset[int] = frozenset({132})


def _enemy_region_name(enemy_number: int, enemy_name: str, specifier: str, room: str) -> str:
    """
    Return the region name for one enemy instance.

    The enemy_number makes the name unique across the game, because enemy_name and specifier
    together are unique only within a room.
    """
    return f"Enemy: {enemy_name}{specifier} ({room}#{enemy_number})"

def create_regions(world: CVAOSWorld) -> None:
    """
    Create all regions for Castlevania: Aria of Sorrow, and the connections between them.

    This creates:
    1. A "Menu" region, connected to the starting door 000:003
    2. An entrance region for each door, plus one for each portal node named only in the
       transdoor overrides
    3. A pickup region for each pickup, leaving out the Hard-Mode-only pickups unless
       hard_mode_pickups_included allows them
    4. An enemy region for each instance of LOGIC_ENEMY_TYPES, except the one-time instances in
       ONE_TIME_ENEMY_NUMBERS
    5. A defeat region for each enemy type whose defeat is a check (enemy_check_types)
    6. Connections from the transdoor table, the routing CSVs, and the special rules in this
       module

    It also stores on ``world`` the region-name indexes that the can_reach_* functions read, and
    registers the indirect conditions of every rule that reads region reachability.
    """
    multiworld = world.multiworld
    player = world.player

    # Create the Menu region (required starting point)
    menu = Region("Menu", player, multiworld)
    multiworld.regions.append(menu)

    # Track the entrance and pickup regions by identifier
    entrance_regions: dict[int, Region] = {}
    """e.g. door_number (1, 2, 3...) -> Region object"""  # pylint: disable=W0105
    pickup_regions: dict[str, Region] = {}
    """e.g. pickup_identifier -> Region object"""  # pylint: disable=W0105

    # Build lookups for routing connections
    # door_identifier_unique -> door_number
    door_id_unique_to_number: dict[str, int] = {
        e.door_identifier_unique: e.door_number for e in entrance_info_collection
    }
    # door_number -> door_identifier_unique
    door_number_to_id_unique: dict[int, str] = {
        e.door_number: e.door_identifier_unique for e in entrance_info_collection
    }

    # Create one entrance region per door, keyed by door_number and named by
    # door_identifier_unique
    for entrance_info in entrance_info_collection:
        region_name = f"Entrance: {entrance_info.door_identifier_unique}"
        region = Region(region_name, player, multiworld)
        entrance_regions[entrance_info.door_number] = region
        multiworld.regions.append(region)

    # Non-standard connections from the override CSV can name door nodes that are not in
    # entrance_info, such as the Chaotic Realm portals 50E<->B20 and B0B<->B14.
    # entrance_region_for resolves a door identifier to its region, and with create=True it
    # creates a region for such a node.
    extra_entrance_regions: dict[str, Region] = {}

    def entrance_region_for(door_id: str, *, create: bool) -> Region | None:
        number = door_id_unique_to_number.get(door_id)
        if number is not None:
            return entrance_regions.get(number)
        region = extra_entrance_regions.get(door_id)
        if region is None and create:
            region = Region(f"Entrance: {door_id}", player, multiworld)
            extra_entrance_regions[door_id] = region
            multiworld.regions.append(region)
        return region

    # Create pickup regions (one per pickup in the game). Hard-Mode-only pickups (HARD_PICKUP
    # entities) are created only when hard_mode_pickups_included returns True, and
    # items.create_itempool applies the same test to the item pool. Routing connections to a
    # skipped pickup resolve to None and are dropped below.
    include_hard = hard_mode_pickups_included(world.options)
    for pickup_info in pickup_info_collection:
        if pickup_info.is_hard_mode_only and not include_hard:
            continue
        region_name = f"Pickup: {pickup_info.display_name}"
        region = Region(region_name, player, multiworld)
        pickup_regions[pickup_info.identifier_key] = region
        multiworld.regions.append(region)

        # Add the pickup as a location in its region (friendly name; see PickupInfo.location_name)
        location_name = pickup_info.location_name
        location_id = location_name_to_id[location_name]
        location = CVAOSLocation(player, location_name, location_id, region)
        region.locations.append(location)

    # Connect Menu to the starting entrance (000:003)
    start_entrance_id = "000:003"
    start_door_number = door_id_unique_to_number.get(start_entrance_id)
    if start_door_number is not None and start_door_number in entrance_regions:
        start_region = entrance_regions[start_door_number]
        menu.connect(start_region, "Start Game")
    elif entrance_info_collection:
        # Fallback if the starting entrance isn't found
        first_entrance = entrance_info_collection[0]
        start_region = entrance_regions[first_entrance.door_number]
        menu.connect(start_region, "Start Game")

    # Connect doors between rooms using the transdoor mapping.
    special_gate_entrances: list[tuple[tuple[str, str], Entrance]] = []
    seen_transdoors: set[tuple[str, str]] = set()
    for transdoor in transdoor_connection_collection:
        key = (transdoor.from_entrance, transdoor.to_entrance)
        if key in seen_transdoors:
            continue
        seen_transdoors.add(key)

        # Create entrance regions on demand for non-standard nodes (portals).
        from_region = entrance_region_for(transdoor.from_entrance, create=True)
        to_region = entrance_region_for(transdoor.to_entrance, create=True)

        if from_region is None or to_region is None:
            continue

        # A few door crossings have a bespoke requirement that is not an ability, such as the
        # 507 -> 506 gate toward the Chaotic Realm. The rule applies in one direction only, so
        # the reverse crossing is free.
        rule_factory = SPECIAL_TRANSDOOR_RULES.get(key)
        access_rule = rule_factory(world) if rule_factory else None

        connection_name = f"Door: {transdoor.from_entrance} -> {transdoor.to_entrance}"
        entrance = from_region.connect(to_region, connection_name, access_rule)
        if rule_factory is not None:
            special_gate_entrances.append((key, entrance))

    # Entrances whose access rule reads whether a Devil or Manticore enemy region is reachable
    # (_tform_available). They are registered as indirect conditions of those regions at the
    # end, once the enemy regions exist.
    tform_gated_entrances: list[Entrance] = []

    # Connect door nodes within each room from the routing CSV. Each row describes one crossing
    # of a room, from the node "{RoomID}:{From}" of the door Soma came in by to the node
    # "{RoomID}:{To}" of the door he leaves by.
    for routing_info in entrance_to_entrance_info_collection:
        room_id = routing_info.room_id
        from_door_id = f"{room_id}:{routing_info.from_room}"
        to_door_id = f"{room_id}:{routing_info.to_room}"

        # Resolve both door nodes, including any portal nodes created above, and skip the row if
        # either is unknown. Routing rows name each door by its unique identifier, because the
        # loader rejects a bare identifier that two doors share.
        from_region = entrance_region_for(from_door_id, create=False)
        to_region = entrance_region_for(to_door_id, create=False)

        if from_region is None or to_region is None:
            # Skip connections where we can't find both regions
            continue

        # Include connection_number to handle multiple routing entries between the same doors
        connection_name = f"{from_door_id} -> {to_door_id} #{routing_info.connection_number}"

        # Create an access rule based on the routing requirements
        access_rule = _create_access_rule_from_routing(routing_info, world)

        # Bespoke per-direction override (e.g. the Forbidden Area unlock), keyed by
        # (room, from, to). The factory returns a replacement rule, or None to keep the CSV rule.
        _room_override = SPECIAL_ROOM_ENTRANCE_RULES.get(
            (room_id, routing_info.from_room, routing_info.to_room))
        if _room_override is not None:
            _replacement = _room_override(world, access_rule)
            if _replacement is not None:
                access_rule = _replacement

        # Connect the regions
        entrance = from_region.connect(to_region, connection_name, access_rule)
        if _requires_tform(routing_info):
            tform_gated_entrances.append(entrance)

    # Special within-room routing for the portal nodes. It is defined in code
    # (SPECIAL_ROOM_ROUTING) rather than in entrance_to_entrance_requirements.csv, because that
    # CSV is regenerated from game data and keyed to entrance_info, which has no portal nodes.
    # The transdoor loop above created the portal nodes, so entrance_region_for resolves them.
    for room_id, from_neighbor, to_neighbor, abilities in SPECIAL_ROOM_ROUTING:
        from_region = entrance_region_for(f"{room_id}:{from_neighbor}", create=False)
        to_region = entrance_region_for(f"{room_id}:{to_neighbor}", create=False)
        if from_region is None or to_region is None:
            continue
        access_rule = _special_room_access_rule(abilities, world)
        connection_name = f"{room_id}:{from_neighbor} -> {room_id}:{to_neighbor} (special)"
        entrance = from_region.connect(to_region, connection_name, access_rule)
        if "Tform" in abilities:
            tform_gated_entrances.append(entrance)

    # Build pickup_number -> identifier_key lookup
    pickup_number_to_identifier: dict[int, str] = {
        p.pickup_number: p.identifier_key for p in pickup_info_collection
    }

    # Connect entrance regions to pickup regions
    for pickup_routing in entrance_to_pickup_region_info_collection:
        # Pickup routing also names each entrance by its unique door identifier
        entrance_id = pickup_routing.entrance_identifier
        door_number = door_id_unique_to_number.get(entrance_id)

        if door_number is None:
            # Skip if we can't find this entrance
            continue

        entrance_region = entrance_regions.get(door_number)
        if entrance_region is None:
            continue

        # Look up the pickup region by pickup_number
        identifier_key = pickup_number_to_identifier.get(pickup_routing.pickup_number)
        if identifier_key is None:
            continue

        pickup_region = pickup_regions.get(identifier_key)
        if pickup_region is None:
            continue

        # Create connection name using pickup_number to ensure uniqueness
        entrance_id_unique = door_number_to_id_unique[door_number]
        connection_name = f"{entrance_id_unique} -> Pickup:{identifier_key} #{pickup_routing.pickup_number}"

        # Create access rule
        access_rule = _create_access_rule_from_routing(pickup_routing, world)

        # Connect entrance to pickup
        entrance = entrance_region.connect(pickup_region, connection_name, access_rule)
        if _requires_tform(pickup_routing):
            tform_gated_entrances.append(entrance)

    # Create one enemy region per instance of LOGIC_ENEMY_TYPES, except the one-time instances,
    # and connect each from its entrances. Enemy regions hold no locations. They exist only so
    # logic can test whether the player can reach an enemy that drops a given soul (see
    # can_reach_any_enemy). They are keyed by enemy_number, which is unique across the game.
    enemy_regions: dict[int, Region] = {}
    enemy_region_name_by_number: dict[int, str] = {}
    for enemy_number, (enemy_name, specifier, enemy_room) in enemy_meta_by_number.items():
        if enemy_name not in LOGIC_ENEMY_TYPES or enemy_number in ONE_TIME_ENEMY_NUMBERS:
            continue
        region_name = _enemy_region_name(enemy_number, enemy_name, specifier, enemy_room)
        region = Region(region_name, player, multiworld)
        enemy_regions[enemy_number] = region
        enemy_region_name_by_number[enemy_number] = region_name
        multiworld.regions.append(region)

    # Expose the name index on the world so can_reach_any_enemy can resolve enemy_number ->
    # region name for this generation.
    world.enemy_region_name_by_number = enemy_region_name_by_number

    for enemy_routing in entrance_to_enemy_region_info_collection:
        if enemy_routing.enemy_name not in LOGIC_ENEMY_TYPES:
            continue
        enemy_region = enemy_regions.get(enemy_routing.enemy_number)
        if enemy_region is None:
            continue

        door_number = door_id_unique_to_number.get(enemy_routing.entrance_identifier)
        if door_number is None:
            continue
        entrance_region = entrance_regions.get(door_number)
        if entrance_region is None:
            continue

        entrance_id_unique = door_number_to_id_unique[door_number]
        connection_name = f"{entrance_id_unique} -> Enemy:{enemy_routing.enemy_number}"
        access_rule = _create_access_rule_from_routing(enemy_routing, world)
        entrance = entrance_region.connect(enemy_region, connection_name, access_rule)
        if _requires_tform(enemy_routing):
            tform_gated_entrances.append(entrance)

    # Index entrance regions by room id (the part before ':') so can_reach_room can ask
    # whether any door of a room is reachable. Includes the auto-created portal nodes.
    entrance_regions_by_room: dict[str, list[Region]] = {}
    for door_id, number in door_id_unique_to_number.items():
        region = entrance_regions.get(number)
        if region is not None:
            entrance_regions_by_room.setdefault(door_id.split(":", 1)[0], []).append(region)
    for door_id, region in extra_entrance_regions.items():
        entrance_regions_by_room.setdefault(door_id.split(":", 1)[0], []).append(region)
    world.entrance_region_names_by_room = {
        room: [region.name for region in regions] for room, regions in entrance_regions_by_room.items()}

    _create_enemy_checks(world, entrance_regions_by_room,
                         lambda door_id: entrance_region_for(door_id, create=False),
                         tform_gated_entrances)

    # An entrance whose access_rule reads region reachability (can_reach_*) must be registered
    # as an indirect condition of those regions. Otherwise a sweep that reaches those regions
    # after checking the entrance leaves it blocked until the state next changes. Do this for
    # the special gates.
    for gate_key, gate_entrance in special_gate_entrances:
        deps_fn = SPECIAL_TRANSDOOR_DEPS.get(gate_key)
        if deps_fn is None:
            continue
        for region_name in deps_fn(world):
            multiworld.register_indirect_condition(
                multiworld.get_region(region_name, player), gate_entrance)

    # ... and for the Tform-gated entrances, whose rules read Devil/Manticore enemy-region
    # reachability (_tform_available).
    tform_regions = [enemy_regions[number] for number, (enemy_name, _, _) in
                     enemy_meta_by_number.items()
                     if enemy_name in TFORM_ENEMY_TYPES and number in enemy_regions]
    for entrance in tform_gated_entrances:
        for region in tform_regions:
            multiworld.register_indirect_condition(region, entrance)


def _create_enemy_checks(world: CVAOSWorld, entrance_regions_by_room: dict[str, list[Region]],
                         door_region: Callable[[str], Region | None],
                         tform_gated_entrances: list[Entrance]) -> None:
    """Create a region for each enemy type whose defeat is a check under the world's options.

    Each region holds its type's defeat location, and every placement of the type connects to it.
    A placement with routing connects from each entrance its routing names, under that routing's
    rule. A boss in its arena (ARENA_PLACEMENTS) connects from every door of its room with no rule.
    Any other placement without routing is not annotated yet, so it connects from every door of its
    room and requires FALLBACK_SOULS. A placement that spawns only after a boss elsewhere also
    requires reaching that boss's room. The checks of EXCLUDED_TYPES are excluded locations.
    """
    types = enemy_check_types(world.options)
    if not types:
        return
    multiworld, player = world.multiworld, world.player

    # Each rule names what it needs in ``uses``, as the routing rules do, for Universal Tracker's
    # routes (tracker_route). While UT rebuilds the world, the fallback also passes with
    # UT_TRICKS_ITEM. UT then shows an unrouted check as reachable with tricks once its room is
    # reachable, because players usually make these checks long before logic expects them.
    tricks_tier = _tracker_shows_tricks(world)

    def fallback(state: CollectionState) -> bool:
        return (state.has_all(FALLBACK_SOULS, player)
                or (tricks_tier and state.has(UT_TRICKS_ITEM, player)))

    def fallback_uses(state: CollectionState) -> tuple[str, ...]:
        if (tricks_tier and state.has(UT_TRICKS_ITEM, player)
                and not state.has_all(FALLBACK_SOULS, player)):
            return ("the route to this enemy isn't recorded",)
        return FALLBACK_SOULS

    fallback.uses = fallback_uses

    def after_boss(rule: Callable[[CollectionState], bool] | None,
                   boss_room: str | None) -> Callable[[CollectionState], bool] | None:
        if boss_room is None:
            return rule

        def gated(state: CollectionState) -> bool:
            return (rule is None or rule(state)) and can_reach_room(state, world, boss_room)

        rule_uses = getattr(rule, "uses", lambda state: ())
        gated.uses = lambda state: (*rule_uses(state), f"beating the boss in room {boss_room}")
        return gated

    for enemy in types:
        region = Region(f"Defeat: {enemy.name}", player, multiworld)
        multiworld.regions.append(region)
        location_name = defeat_location_name(enemy.name)
        location = CVAOSLocation(player, location_name, location_name_to_id[location_name], region)
        if enemy.name in EXCLUDED_TYPES:
            location.progress_type = LocationProgressType.EXCLUDED
        region.locations.append(location)
        for placement in placements_of(enemy.name):
            boss_room = SPAWNS_AFTER_BOSS_IN.get(placement.enemy_number)
            routing = by_enemy_number_for_enemy_regions.get(placement.enemy_number)
            if placement.enemy_number in ARENA_PLACEMENTS:
                ways = [(door, door.name, None, False)
                        for door in entrance_regions_by_room.get(placement.room, ())]
            elif routing:
                ways = [(door_region(row.entrance_identifier), row.entrance_identifier,
                         _create_access_rule_from_routing(row, world), _requires_tform(row))
                        for row in routing]
            else:
                ways = [(door, door.name, fallback, False)
                        for door in entrance_regions_by_room.get(placement.room, ())]
            for from_region, from_name, rule, needs_tform in ways:
                if from_region is None:
                    continue
                entrance = from_region.connect(
                    region, f"{from_name} -> Defeat:{enemy.name} #{placement.enemy_number}",
                    after_boss(rule, boss_room))
                if needs_tform:
                    tform_gated_entrances.append(entrance)
                for boss_door in entrance_regions_by_room.get(boss_room, ()) if boss_room else ():
                    multiworld.register_indirect_condition(boss_door, entrance)


# Soul-ability bit -> the AP item granting it.
_ABILITY_TO_ITEM: dict[AbilityCombo, str] = {
    AbilityCombo.Glide: "Flying Armor",
    AbilityCombo.Slide: "Skeleton Blaze",
    AbilityCombo.DJump: "Malphas",
    AbilityCombo.HJump: "Hippogryph",
    AbilityCombo.WWalk: "Undine",
    AbilityCombo.Dive: "Skula",
    AbilityCombo.Panth: "Black Panther",
    AbilityCombo.Bat: "Giant Bat",
    AbilityCombo.BDash: "Grave Keeper",
    # Kicker Skeleton has no pickup or location, because its soul is only a rare enemy drop
    # (rate 2). No item has this name, so every Kick route stays out of logic. Putting Kick in
    # logic would need an enemy region for the Kicker Skeleton in room 505, checked the way Tform
    # checks the Devil and Manticore regions. It would also ask players to farm that drop for an
    # optional route.
    AbilityCombo.Kick: "Kicker Skeleton",
    AbilityCombo.Galamoth: "Galamoth",
    # Chronomage has no pickup or location, because Galamoth's soul does essentially the same
    # thing. This entry may need to change if the Chronomage soul is ever shuffled, or if Sky
    # Fish becomes one of the three Ancient Book souls.
    AbilityCombo.Chronomage: "Chronomage",
}

_SOUL_BITS: int = int(functools.reduce(operator.or_, _ABILITY_TO_ITEM))

# Ceil / Floor are satisfied by holding any item that breaks ceilings / floors
_CEILING_BREAKERS: frozenset[str] = frozenset(
    info.name for info in item_info_collection if info.breaks_ceilings)
_FLOOR_BREAKERS: frozenset[str] = frozenset(
    info.name for info in item_info_collection if info.breaks_floors)
# Oops! All Whips gives every weapon the Whip Sword's swing, and the Whip Sword breaks neither
# ceilings nor floors. With that option on, only the breakers that are not weapons count.
_WEAPON_NAMES: frozenset[str] = frozenset(
    info.name for info in item_info_collection if info.item_category == "weapon")


def _breakers(world: CVAOSWorld) -> tuple[frozenset[str], frozenset[str]]:
    """Return the items that break ceilings in ``world``, then the items that break floors."""
    if world.options.oops_all_whips:
        return _CEILING_BREAKERS - _WEAPON_NAMES, _FLOOR_BREAKERS - _WEAPON_NAMES
    return _CEILING_BREAKERS, _FLOOR_BREAKERS
_BREAK_BITS: int = int(AbilityCombo.Ceil | AbilityCombo.Floor)

# TODO: implement Vert's real requirement, then move it out of here.
_ROOM_TECHNIQUE_BITS: int = 0  # would be int(AbilityCombo.Vert) once it has a requirement

# Universal Tracker marks a location as "glitched" when it becomes reachable once UT collects the
# item named by the world's glitches_item_name. While UT rebuilds the world, a way through whose
# only out-of-logic requirements are tricks also requires this item, so the locations past it are
# marked as glitched. A normal generation never builds these ways.
UT_TRICKS_ITEM = "Out-of-Logic Tricks"
_TRICK_BITS: int = int(functools.reduce(operator.or_, (technique.bit for technique in TECHNIQUES)))


def _tracker_shows_tricks(world: CVAOSWorld) -> bool:
    """Whether Universal Tracker is rebuilding ``world``, which is when the tricks tier is built."""
    passthrough = getattr(getattr(world, "multiworld", None), "re_gen_passthrough", {})
    return bool(passthrough.get(getattr(world, "game", None)))

# Tform (transformation) is satisfied when the player can farm a transformation soul, which means
# reaching any TFORM_ENEMY_TYPES enemy region
_TFORM_BIT: int = int(AbilityCombo.Tform)


def _create_access_rule_from_routing(
    routing_info,
    world: CVAOSWorld,
) -> Callable[[CollectionState], bool] | None:
    """
    Create an access rule function from RoutingInfo.

    Each requirement mask in the RoutingInfo is one way through, and satisfying any one of them
    is enough. Each bit in a mask is one requirement, and a way through needs all of its bits,
    such as DJump and Dive together.

    A soul bit requires the soul that _ABILITY_TO_ITEM names. The Tform bit requires a reachable
    source of a transformation soul (_tform_available). The Ceil and Floor bits require an item
    that breaks ceilings or floors respectively (_breakers, which leaves out the weapons under
    Oops! All Whips).

    Technique bits depend on the resolved logic options (options.resolve_allowed_techniques),
    which are fixed once generation starts, so they are resolved here at build time. A mask that
    requires Impossible or an out-of-logic technique is pruned.

    The exception is a Universal Tracker rebuild. There, a mask whose only out-of-logic
    requirements are tricks (TECHNIQUES bits) goes into a separate tricks tier instead of being
    pruned. A way through in that tier also requires UT_TRICKS_ITEM, so UT shows the locations
    past it as reachable with tricks. A mask with any other out-of-logic requirement, such as
    Impossible, is pruned even then.

    Returns None when some in-logic way through requires nothing (always accessible), an
    always-False rule when every way through is out of logic and none is in the tricks tier,
    and a per-state rule otherwise. The per-state rule also has a ``uses`` attribute, which
    returns what the simplest usable way through needs, for Universal Tracker's routes.
    """
    requirement_masks = routing_info.get_requirement_bitmasks()

    if not requirement_masks:
        # No requirements means always accessible
        return None

    in_logic_bits = int(resolve_allowed_techniques(world.options)) | _ROOM_TECHNIQUE_BITS
    tricks_tier = _tracker_shows_tricks(world)

    needs: list[tuple[tuple[str, ...], bool, bool, bool]] = []
    trick_needs: list[tuple[tuple[str, ...], bool, bool, bool]] = []
    for mask in map(int, requirement_masks):
        items = tuple(item for bit, item in _ABILITY_TO_ITEM.items() if mask & bit)
        need = (items, bool(mask & _TFORM_BIT), bool(mask & int(AbilityCombo.Ceil)),
                bool(mask & int(AbilityCombo.Floor)))
        out_of_logic = mask & ~_SOUL_BITS & ~in_logic_bits & ~_TFORM_BIT & ~_BREAK_BITS
        if out_of_logic:
            # Demands Impossible or an out-of-logic technique. If every out-of-logic requirement
            # is a trick, the way goes into UT's tricks tier, which requires the tricks item.
            if tricks_tier and not out_of_logic & ~_TRICK_BITS:
                trick_needs.append(need)
            continue
        if not items and not any(need[1:]):
            return None  # needs nothing gateable: always open
        needs.append(need)

    if not needs and not trick_needs:
        return lambda state: False  # every way through is out of logic at these options

    # Masks that differ only in resolved technique bits give the same need, so drop duplicates.
    resolved_needs = tuple(dict.fromkeys(needs))
    resolved_trick_needs = tuple(dict.fromkeys(trick_needs))
    player = world.player
    ceiling_breakers, floor_breakers = _breakers(world)

    def satisfied(state: CollectionState, need: tuple[tuple[str, ...], bool, bool, bool]) -> bool:
        items, needs_tform, needs_ceiling, needs_floor = need
        return (state.has_all(items, player)
                and (not needs_tform or _tform_available(state, world))
                and (not needs_ceiling or state.has_any(ceiling_breakers, player))
                and (not needs_floor or state.has_any(floor_breakers, player)))

    def access_rule(state: CollectionState) -> bool:
        return (any(satisfied(state, need) for need in resolved_needs)
                or (bool(resolved_trick_needs) and state.has(UT_TRICKS_ITEM, player)
                    and any(satisfied(state, need) for need in resolved_trick_needs)))

    def uses(state: CollectionState) -> tuple[str, ...]:
        """Return what the simplest way through needs, among the ways that ``state`` can use.

        Each need is worded as a route shows it, such as "a transformation soul". A way without a
        trick is simpler than any way with one, and otherwise the way with fewer needs is simpler.
        """
        ways = [(False, need) for need in resolved_needs if satisfied(state, need)]
        if state.has(UT_TRICKS_ITEM, player):
            ways += [(True, need) for need in resolved_trick_needs if satisfied(state, need)]
        if not ways:
            return ()

        def cost(way: tuple[bool, tuple[tuple[str, ...], bool, bool, bool]]) -> tuple[bool, int]:
            trick, (items, *flags) = way
            return trick, len(items) + sum(flags)

        trick, need = min(ways, key=cost)
        items, needs_tform, needs_ceiling, needs_floor = need
        used = list(items)
        if needs_tform:
            used.append("a transformation soul")
        if needs_ceiling:
            used.append(f"{_first_held(state, ceiling_breakers, player)} to break the ceiling")
        if needs_floor:
            used.append(f"{_first_held(state, floor_breakers, player)} to break the floor")
        if trick:
            used.append("a trick")
        return tuple(used)

    access_rule.uses = uses  # Universal Tracker's routes name what each step needs (tracker_route)
    return access_rule


def _first_held(state: CollectionState, names: frozenset[str], player: int) -> str:
    """Return the first of ``names``, alphabetically, that ``state`` holds."""
    return min(name for name in names if state.has(name, player))


def _requires_tform(routing_info) -> bool:
    """
    Return whether any of the routing's requirement masks includes the Tform bit.

    create_regions registers such a connection's entrance as an indirect condition of the
    TFORM_ENEMY_TYPES enemy regions, because its rule reads whether they are reachable.
    """
    return any(int(mask) & _TFORM_BIT for mask in routing_info.get_requirement_bitmasks())


def _tform_available(state: CollectionState, world: CVAOSWorld) -> bool:
    """
    Return whether a transformation-soul source is reachable, meaning any Devil or Manticore
    enemy region (TFORM_ENEMY_TYPES).
    """
    return any(can_reach_any_enemy(state, world, name) for name in TFORM_ENEMY_TYPES)


def can_reach_any_enemy(state: CollectionState, world: CVAOSWorld, enemy_name: str) -> bool:
    """
    Return whether the player can reach *any* instance of ``enemy_name``.

    Only LOGIC_ENEMY_TYPES have enemy regions, so any other name raises ValueError. The one-time
    instances in ONE_TIME_ENEMY_NUMBERS have no region, so they never count.
    """
    if enemy_name not in LOGIC_ENEMY_TYPES:
        raise ValueError(
            f"{enemy_name!r} has no enemy regions; add it to LOGIC_ENEMY_TYPES first")
    name_by_number = world.enemy_region_name_by_number
    return any(
        state.can_reach_region(name_by_number[number], world.player)
        for number in by_enemy_name_for_enemy_regions.get(enemy_name, set())
        if number in name_by_number
    )


def can_reach_room(state: CollectionState, world: CVAOSWorld, room_id: str) -> bool:
    """
    Return whether the player can reach room ``room_id``, meaning any of its door regions.

    The goals use it for the Chaos room "B14" and Graham's room "904", and the Chaotic Realm gate
    uses it for room 904 too. Enemy checks use it for an enemy that spawns only after a boss.
    """
    return any(
        state.can_reach_region(region_name, world.player)
        for region_name in world.entrance_region_names_by_room.get(room_id, ())
    )

ANCIENT_BOOKS = ("Ancient Book 1", "Ancient Book 2", "Ancient Book 3")

def _chaotic_realm_gate(world: CVAOSWorld):
    """
    Return the access rule for the two true-ending door crossings.

    The rule applies to the 507 -> 506 door (Julius) and to the 904 -> 900 door, which leads from
    Graham's room onto the Balcony. The reverse crossings are free.

    The rule requires all three Ancient Books and the Giant Bat soul (a pickup). It also requires
    reaching a Flame Demon and a Succubus, whose souls are enemy drops, and reaching Graham's
    room, 904.

    For randomization, reaching room 904 while those souls are obtainable counts as having beaten
    Graham with them, so can_reach_room("904") stands for defeating Graham. Crossing from 507
    into 506, and therefore reaching and defeating Chaos, requires all of the above.
    """
    player = world.player

    def rule(state: CollectionState) -> bool:
        return (
            state.has_all(ANCIENT_BOOKS, player)
            and state.has("Giant Bat", player)
            and can_reach_any_enemy(state, world, "Flame Demon")
            and can_reach_any_enemy(state, world, "Succubus")
            and can_reach_room(state, world, "904")
        )

    rule.uses = lambda state: ("the three Ancient Books", "Giant Bat", "a Flame Demon soul",
                               "a Succubus soul", "beating Graham")
    return rule


def _chaotic_realm_gate_deps(world: CVAOSWorld) -> list[str]:
    """Region names the gate rule reads, to register as indirect conditions: room 904's
    doors (the can_reach_room("904") term) and the Flame Demon / Succubus enemy regions."""
    names = list(world.entrance_region_names_by_room.get("904", []))
    for enemy_name in ("Flame Demon", "Succubus"):
        for number in by_enemy_name_for_enemy_regions.get(enemy_name, ()):
            region_name = world.enemy_region_name_by_number.get(number)
            if region_name:
                names.append(region_name)
    return names


# Bespoke, non-ability access rules on specific *directional* transdoor crossings, keyed by
# (from_entrance, to_entrance). Consulted in create_regions' transdoor loop. ``RULES`` values
# are factories returning the access-rule callable; ``DEPS`` values return the region names
# that rule reads, for indirect-condition registration. Like the CSV rules, each access rule has a
# ``uses`` attribute, so Universal Tracker's routes name what the crossing needs (tracker_route).
SPECIAL_TRANSDOOR_RULES: dict[tuple[str, str], object] = {
    ("507:506", "506:507"): _chaotic_realm_gate,   # 507 -> 506: toward Chaos (B14)
    ("904:900", "900:904"): _chaotic_realm_gate,   # 904 -> 900: from Graham's room onto the Balcony
}
SPECIAL_TRANSDOOR_DEPS: dict[tuple[str, str], object] = {
    ("507:506", "506:507"): _chaotic_realm_gate_deps,
    ("904:900", "900:904"): _chaotic_realm_gate_deps,
}


def _forbidden_area_forward_gate(world: CVAOSWorld, _csv_rule):
    """
    Return the rule for the route 20D -> A02 in room A01, down from the Study to the Forbidden Area.

    With ForbiddenAreaButton set to pickup, the barrier opens once the shuffled "Study Sealswitch"
    is obtained (it sets misc #48), so this direction requires only the Sealswitch.

    With the button left in place, this returns None to keep the CSV-derived rule.

    The reverse direction is handled by _forbidden_area_reverse_gate.
    """
    use_csv_logic_options = frozenset({ForbiddenAreaButton.option_off})
    if world.options.forbidden_area_button.value in use_csv_logic_options:
        return None
    player = world.player

    def rule(state: CollectionState) -> bool:
        return state.has("Study Sealswitch", player)

    rule.uses = lambda state: ("Study Sealswitch",)
    return rule


def _forbidden_area_reverse_gate(world: CVAOSWorld, csv_rule):
    """
    Return the rule for the route A02 -> 20D in room A01, up from the Forbidden Area to the Study.

    The barrier blocks this direction too. In the vanilla game the button beside the A02 hole opens
    it, which is why the CSV rule asks only for a way up. With the button turned into a pickup, the
    barrier opens only once the Study Sealswitch is received, so the route needs the Sealswitch as
    well as the CSV rule. With the button left in place, this returns None to keep the CSV rule.
    """
    if world.options.forbidden_area_button.value == ForbiddenAreaButton.option_off:
        return None
    player = world.player
    csv_uses = getattr(csv_rule, "uses", lambda state: ())

    def rule(state: CollectionState) -> bool:
        return state.has("Study Sealswitch", player) and (csv_rule is None or csv_rule(state))

    rule.uses = lambda state: ("Study Sealswitch", *csv_uses(state))
    return rule


# Bespoke per-direction access rules on specific WITHIN-ROOM crossings, keyed by
# (room_id, from_room, to_room). Consulted in create_regions' within-room routing loop. A factory
# takes the world and the CSV-derived rule, and returns the replacement access-rule callable, or
# None to keep the CSV rule. (has-item rules need no indirect-condition registration, unlike the
# can_reach transdoor gates.) A replacement rule has a ``uses`` attribute, like the CSV rule it
# replaces, so Universal Tracker's routes name what the crossing needs (tracker_route).
SPECIAL_ROOM_ENTRANCE_RULES: dict[tuple[str, str, str], object] = {
    ("A01", "20D", "A02"): _forbidden_area_forward_gate,
    ("A01", "A02", "20D"): _forbidden_area_reverse_gate,
}


class _StaticRouting:
    """Minimal stand-in exposing get_requirement_bitmasks so _create_access_rule_from_routing
    can build an access rule from an in-code requirement mask."""

    def __init__(self, masks: tuple[int, ...]) -> None:
        self._masks = masks

    def get_requirement_bitmasks(self, **_) -> tuple[int, ...]:
        return self._masks


def _special_room_access_rule(ability_names: tuple[str, ...], world: CVAOSWorld):
    """
    Build an access rule for a special within-room route from AbilityCombo names.

    An empty tuple means the route is free, so this returns None.

    Only "Vert" is used so far, and it has no requirement yet (_ROOM_TECHNIQUE_BITS). The
    access-rule logic therefore prunes it, so the rule is always False and the route stays out
    of logic until Vert's requirement is implemented.

    TODO: revisit then.
    """
    if not ability_names:
        return None
    mask = 0
    for name in ability_names:
        mask |= int(getattr(AbilityCombo, name))
    return _create_access_rule_from_routing(_StaticRouting((mask,)), world)


# Within-room routing for the portal door nodes, which create_regions connects. Each entry is
# (room_id, from_neighbor, to_neighbor, required AbilityCombo names). It is kept in code so the
# regenerated entrance_to_entrance_requirements.csv stays keyed to entrance_info.
SPECIAL_ROOM_ROUTING: list[tuple[str, str, str, tuple[str, ...]]] = [
    # Room 50E: the 506 door <-> the B20 portal node, free both ways.
    ("50E", "506", "B20", ()),
    ("50E", "B20", "506", ()),
    # Room B20: from the 50E portal arrival to the room's exits. Vanilla needs a vertical
    # portal jump (Vert) to reach B1F; the other exits are free.
    ("B20", "50E", "B1F", ("Vert",)),
    ("B20", "B1F", "50E", ("Vert",)),
    ("B20", "50E", "B1E", ()),
    ("B20", "B1E", "50E", ()),
    ("B20", "50E", "B27", ()),
    ("B20", "B27", "50E", ()),
    # Room B0B: the B0D door <-> the B14 portal node, free both ways.
    ("B0B", "B0D", "B14", ()),
    ("B0B", "B14", "B0D", ()),
]
