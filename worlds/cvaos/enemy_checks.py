"""
Enemy checks: one location per kind of enemy, checked the first time that kind is defeated.

The game's Enemy List keeps a saved bit for each enemy type, and the death routine sets a type's
bit on its first defeat (EWRAM 0x020133A0, bit ``id & 7`` of byte ``id >> 3``). The client
reads those bits, so detecting a defeat needs no ROM change, and a defeat made offline is sent on
the next connection. The ROM never hands out the item at an enemy check. The client gets it from
the server instead, so with enemy checks on, the client also asks the server for the player's own
items (see :func:`uses_enemy_checks`).

Logic treats a type's check as reachable when any of its placements is. A placement with routing
in the enemy routing CSV uses that routing. One without routing has not been annotated yet, so
logic expects :data:`FALLBACK_SOULS` once its room is reachable. Boss Rush arenas never count as
placements.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .data import (EnemyPlacement, EnemyType, by_enemy_number_for_enemy_regions,
                   enemy_placements, enemy_type_collection)

if TYPE_CHECKING:
    from .options import CVAOSOptions

__all__ = [
    "BOSS_TYPES",
    "FALLBACK_SOULS",
    "DEFEAT_ID_BASE",
    "defeat_location_name",
    "defeat_location_id",
    "enemy_check_types",
    "enemy_id_for_location",
    "has_recorded_route",
    "pin_rooms",
    "placements_of",
    "uses_enemy_checks",
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

# What logic expects before the check of an enemy whose placements have no routing yet: these
# eight mobility souls.
FALLBACK_SOULS: tuple[str, ...] = ("Flying Armor", "Skeleton Blaze", "Malphas", "Hippogryph",
                                   "Undine", "Skula", "Black Panther", "Giant Bat")

# Room ids in the Boss Rush area, whose arenas never count as placements.
BOSS_RUSH_AREA = "C"

# Rooms whose placements spawn only once a boss elsewhere has been beaten, mapped to that boss's
# room. Three of room 710's four placements appear only after Legion (room 725). The data doesn't
# say which three, so logic requires beating Legion for all four. Alura Une, Biphron and Ripper
# all appear in other rooms too, so each of their checks still has placements that don't need
# Legion. A placement that spawns after the boss of its own room needs no entry, because logic
# treats reaching a boss's room as beating that boss.
SPAWNS_AFTER_BOSS_IN: dict[str, str] = {"710": "725"}


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


def uses_enemy_checks(options: "CVAOSOptions") -> bool:
    return options.enemy_checks.value != options.enemy_checks.option_off


def placements_of(enemy_name: str):
    """Return the placements of ``enemy_name`` that count for its check (Boss Rush left out)."""
    return tuple(placement for placement in enemy_placements
                 if placement.name == enemy_name and not placement.room.startswith(BOSS_RUSH_AREA))


def has_recorded_route(enemy_name: str) -> bool:
    """Whether some placement of ``enemy_name`` has routing, so logic needs no fallback for it."""
    return any(placement.enemy_number in by_enemy_number_for_enemy_regions
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
