"""
Which door and placement pairs still need annotating, and what the other doors already imply.

A pair is a door of a room and a placement in the same room. Logic already combines the enemy
tab's rows with the room's door-to-door routing: if door A reaches door B with requirement M, and
B reaches a placement with R, then A reaches it with M and R together. So a pair can be *implied*
by other pairs. A pair whose implied requirement is free can't be improved on, and the worklist
leaves it out.

Placements in Boss Rush and bosses in their arenas (``ARENA_PLACEMENTS``) are left out too, because
logic doesn't use their rows.
"""
from __future__ import annotations

from dataclasses import dataclass

from ...data import routing_info
from ...enemy_checks import ARENA_PLACEMENTS, BOSS_RUSH_AREA
from ...regions import LOGIC_ENEMY_TYPES
from ..import_enemy_requirements import unrouted_types
from .model import EnemyTab

Masks = tuple[int, ...]


def _add(antichain: set[int], mask: int) -> bool:
    """Add ``mask`` to a set of subset-minimal masks. Return whether it was new and not covered."""
    if any(kept | mask == mask for kept in antichain):
        return False
    antichain.difference_update({kept for kept in antichain if kept | mask == kept})
    antichain.add(mask)
    return True


def door_paths(room: str) -> dict[str, dict[str, set[int]]]:
    """Return, for each door of ``room``, the minimal requirements to reach each other door.

    Only routes inside the room count, from ``entrance_to_entrance_requirements.csv``. Each door
    reaches itself with nothing.
    """
    doors = sorted(routing_info._arrivals_by_room.get(room, ()))
    edges: dict[str, list[tuple[str, Masks]]] = {}
    for row in routing_info.rows:
        if row.room_id != room:
            continue
        masks = row.get_requirement_bitmasks(strict_combo_tokens=False)
        if masks:
            edges.setdefault(f"{room}:{row.from_room}", []).append((f"{room}:{row.to_room}", masks))
    out = {}
    for start in doors:
        best: dict[str, set[int]] = {start: {0}}
        queue = [(start, 0)]
        while queue:
            node, mask = queue.pop()
            for target, masks in edges.get(node, ()):
                for edge_mask in masks:
                    if _add(best.setdefault(target, set()), mask | edge_mask):
                        queue.append((target, mask | edge_mask))
        out[start] = best
    return out


@dataclass(frozen=True)
class Pair:
    """One door and placement pair, and what is known about it."""
    door: str            # entrance node, such as "90D:917"
    enemy: int
    state: str           # "annotated", "implied" or "open"
    masks: Masks = ()    # the recorded requirement, or the implied one


def room_pairs(tab: EnemyTab, room: str, recorded: dict[tuple[str, int], Masks],
               paths: dict[str, dict[str, set[int]]] | None = None) -> list[Pair]:
    """Return every pair of ``room`` that logic can use, with its state."""
    if room.startswith(BOSS_RUSH_AREA):
        return []
    paths = paths if paths is not None else door_paths(room)
    doors = sorted(routing_info._arrivals_by_room.get(room, ()))
    enemies = sorted({row.enemy for row in tab.rows
                      if row.values["room_id"].strip() == room
                      and row.enemy not in ARENA_PLACEMENTS})
    out = []
    for enemy in enemies:
        for door in doors:
            if (door, enemy) in recorded:
                out.append(Pair(door, enemy, "annotated", recorded[(door, enemy)]))
                continue
            implied: set[int] = set()
            for via in doors:
                if (via, enemy) not in recorded:
                    continue
                for path_mask in paths.get(door, {}).get(via, ()):
                    for mask in recorded[(via, enemy)] or (0,):
                        _add(implied, path_mask | mask)
            masks = tuple(sorted(implied, key=lambda m: (m.bit_count(), m)))
            out.append(Pair(door, enemy, "implied" if masks else "open", masks))
    return out


@dataclass(frozen=True)
class Item:
    """A placement with doors still to annotate, in the order the worklist offers them."""
    enemy: int
    name: str
    room: str
    open_doors: tuple[str, ...]
    group: str           # "unrouted type", "logic type" or "other"


def worklist(tab: EnemyTab) -> tuple[list[Item], dict[str, tuple[int, int]]]:
    """Return the worklist, and each room's (finished pairs, all pairs).

    A pair is finished when it is annotated or implied free. Items come in three groups: types
    with no routed placement yet (their checks need the fallback souls), then the types logic
    uses for souls, then the rest. Within a group, placements in rooms with fewer open doors
    come first.
    """
    recorded = tab.pair_requirements()
    unrouted = set(unrouted_types(recorded))
    rooms = sorted({row.values["room_id"].strip() for row in tab.rows})
    names = {row.enemy: row.values["Enemy Name"].strip() for row in tab.rows}
    items, progress = [], {}
    for room in rooms:
        pairs = room_pairs(tab, room, recorded)
        if not pairs:
            continue
        done = sum(1 for pair in pairs if pair.state == "annotated"
                   or (pair.state == "implied" and 0 in pair.masks))
        progress[room] = (done, len(pairs))
        open_by_enemy: dict[int, list[str]] = {}
        for pair in pairs:
            if pair.state == "open" or (pair.state == "implied" and 0 not in pair.masks):
                open_by_enemy.setdefault(pair.enemy, []).append(pair.door)
        for enemy, doors in open_by_enemy.items():
            name = names[enemy]
            group = ("unrouted type" if name in unrouted else
                     "logic type" if name in LOGIC_ENEMY_TYPES else "other")
            items.append(Item(enemy, name, room, tuple(doors), group))
    order = {"unrouted type": 0, "logic type": 1, "other": 2}
    items.sort(key=lambda item: (order[item.group], len(item.open_doors), item.room, item.enemy))
    return items, progress
