"""
Enemy checks: one location per kind of enemy, checked the first time that kind is defeated.

The game's Enemy List keeps a saved bit for each enemy type, and the death routine sets a type's
bit on its first defeat (EWRAM 0x020133A0, bit ``id & 7`` of byte ``id >> 3``). The client
reads those bits, so detecting a defeat needs no ROM change, and a defeat made offline is sent on
the next connection. The ROM never gives the item at an enemy check. The client gets it from the
server instead, because it asks the server for the player's own items too
(``client_logic.items_handling_for``).

Logic treats a type's check as reachable when any of its placements is. A placement with routing
in the enemy routing CSV uses that routing. A boss in its own arena (:data:`ARENA_PLACEMENTS`)
needs only a door of that room. Any other placement without routing has not been annotated yet,
so logic expects :data:`FALLBACK_SOULS` once its room is reachable. Boss Rush arenas never count
as placements.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .data import (EnemyPlacement, EnemyType, by_enemy_number_for_enemy_regions,
                   enemy_placements, enemy_type_collection)

if TYPE_CHECKING:
    from .options import CVAOSOptions

__all__ = [
    "ARENA_PLACEMENTS",
    "BOSS_TYPES",
    "FALLBACK_SOULS",
    "DEFEAT_ID_BASE",
    "EXCLUDED_TYPES",
    "defeat_location_name",
    "defeat_location_id",
    "enemy_check_types",
    "enemy_id_for_location",
    "has_recorded_route",
    "pin_rooms",
    "placements_of",
]

# Defeat locations take ids DEFEAT_ID_BASE + enemy id. Pickup locations are numbered by their ROM
# pointer, which is at least 0x08000000, so the two ranges never meet.
DEFEAT_ID_BASE = 0x00E1_0000

# Chaos is never a check: defeating his second form ends the game.
NEVER_CHECKED = frozenset({"Chaos"})

# The types the "bosses" setting checks. Creaking Skull, Manticore, Great Armor and Big Golem also
# appear as ordinary enemies elsewhere, and any of their placements counts.
BOSS_TYPES: tuple[str, ...] = ("Creaking Skull", "Manticore", "Great Armor", "Big Golem",
                               "Headhunter", "Death", "Legion", "Balore", "Belmont", "Graham")

# The eight mobility souls that logic requires for the check of an enemy without a recorded route
# (see has_recorded_route).
FALLBACK_SOULS: tuple[str, ...] = ("Flying Armor", "Skeleton Blaze", "Malphas", "Hippogryph",
                                   "Undine", "Skula", "Black Panther", "Giant Bat")

# The arena placements of the eight bosses that have no routing, by enemy number: Creaking Skull
# (room 00A), Great Armor (20F), Big Golem (313), Headhunter (40B), Belmont (506), Death (607),
# Legion (725) and Balore (801). A boss seals its arena until it is defeated, so logic treats
# reaching a boss's room as beating that boss. The goal and the Chaotic Realm gate rely on the same
# rule. Such a placement therefore needs only a door of its room.
ARENA_PLACEMENTS: frozenset[int] = frozenset({27, 193, 264, 331, 394, 422, 618, 664})

# Types whose checks are excluded locations, which hold only filler. Iron Golem has 9999 HP and
# DEF 255, and both types resist all nine named elements. How long these fights take with
# ordinary gear is not yet measured, so no other game's progression should wait on them.
EXCLUDED_TYPES: frozenset[str] = frozenset({"Iron Golem", "Final Guard"})

# The area id of the Boss Rush rooms, which is the first character of their room ids. Placements in
# those rooms never count for a check.
BOSS_RUSH_AREA = "C"

# Placements that spawn only once a boss elsewhere has been beaten, by enemy number, mapped to that
# boss's room. In room 710, Alura Une (#555) and both Rippers (#556 and #558) appear only after
# Legion (room 725), while Biphron (#557) is always there. Alura Une and Ripper appear in other
# rooms too, so each of their checks still has placements that don't need Legion. A placement that
# spawns after the boss of its own room needs no entry, because logic treats reaching a boss's room
# as beating that boss.
SPAWNS_AFTER_BOSS_IN: dict[int, str] = {555: "725", 556: "725", 558: "725"}


def defeat_location_name(enemy_name: str) -> str:
    return f"Defeat {enemy_name}"


def defeat_location_id(enemy_id: int) -> int:
    return DEFEAT_ID_BASE + enemy_id


def enemy_id_for_location(location_id: int) -> int | None:
    """Return the enemy id a defeat location stands for, or None for any other location."""
    enemy_id = location_id - DEFEAT_ID_BASE
    return enemy_id if 0 <= enemy_id < len(enemy_type_collection) else None


def checkable_types() -> tuple[EnemyType, ...]:
    """Return every type that can have a defeat location, under some option."""
    return tuple(enemy for enemy in enemy_type_collection if enemy.name not in NEVER_CHECKED)


def enemy_check_types(options: "CVAOSOptions") -> tuple[EnemyType, ...]:
    """Return the types whose defeat is a check under ``options``, in enemy id order."""
    from .options import EnemyChecks, Goal

    setting = options.enemy_checks.value
    if setting == EnemyChecks.option_off:
        return ()
    types = checkable_types()
    if setting == EnemyChecks.option_bosses:
        types = tuple(enemy for enemy in types if enemy.name in BOSS_TYPES)
    if options.goal.value == Goal.option_graham:
        # Under the Graham goal, defeating Graham is the goal itself.
        types = tuple(enemy for enemy in types if enemy.name != "Graham")
    return types


def placements_of(enemy_name: str):
    """Return the placements of ``enemy_name`` that count for its check (Boss Rush left out)."""
    return tuple(placement for placement in enemy_placements
                 if placement.name == enemy_name and not placement.room.startswith(BOSS_RUSH_AREA))


def has_recorded_route(enemy_name: str) -> bool:
    """Whether logic has an exact rule for some placement of ``enemy_name``.

    Such a placement either has routing or is a boss in its arena. A type without one needs
    :data:`FALLBACK_SOULS` for its check.
    """
    return any(placement.enemy_number in by_enemy_number_for_enemy_regions
               or placement.enemy_number in ARENA_PLACEMENTS
               for placement in placements_of(enemy_name))


def pin_rooms(enemy_name: str) -> list[tuple[str, EnemyPlacement | None]]:
    """Return each room where the trackers pin ``enemy_name``'s defeat location.

    Defeating the enemy in any room where it appears makes the check, so the check gets a pin in
    each of those rooms. Each room comes with the placement its pin goes at: the room's first
    placement of the enemy with a recorded position. A few placements are recorded at (0, 0), the
    room's top-left corner, rather than where the enemy appears. The placement is None when every
    placement of the enemy in the room is recorded that way.
    """
    rooms: dict[str, EnemyPlacement | None] = {}
    by_number = sorted(placements_of(enemy_name), key=lambda placement: placement.enemy_number)
    for placement in by_number:
        positioned = (placement.x, placement.y) != (0, 0)
        if rooms.get(placement.room) is None:
            rooms[placement.room] = placement if positioned else None
    return list(rooms.items())
