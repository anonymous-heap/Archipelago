"""
Compute the enemy tab's "Shared enemies" and "Shared entrances" columns.

The logic sheet's enemy tab has one row per placement and set of doors. Two rows share a
requirement when their ways through are the same once read the way the loader reads them:
minimized and sorted, so the order of ticks, combos and labels makes no difference. That reduced
form is a row's signature.

For a row with doors S and placement p:

* "Shared enemies" lists the other placements of the room whose requirement from every door in S
  has the same signature.
* "Shared entrances" lists the doors outside S whose requirement to p has the same signature.

Nobody types in these columns. The annotator writes them with every change, and
``import_enemy_requirements`` warns when the sheet's copy is out of date.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping, Sequence

from ..data import routing_info


def signature(row: Mapping[str, str], ability_headers: Iterable[str],
              combo_headers: Iterable[str]) -> tuple[int, ...] | None:
    """Return the row's ways through as sorted, minimized masks, or None when it names none."""
    abilities, combos = routing_info._extract_abilities_and_combo_texts(row, ability_headers,
                                                                        combo_headers)
    if not abilities and not combos:
        return None
    return tuple(sorted(routing_info._requirement_bitmasks_from(abilities, combos,
                                                                strict_combo_tokens=False)))


def doors(row: Mapping[str, str]) -> set[str]:
    """Return the entrance nodes, such as "90D:917", that the row's doors cell names."""
    return routing_info._entrance_identifiers_from_cell((row.get("room_id") or "").strip(),
                                                        row.get("dest_room_identifier"))


def shared_columns(rows: Sequence[Mapping[str, str]], ability_headers: Sequence[str],
                   combo_headers: Sequence[str]) -> list[tuple[str, str]]:
    """Return ("Shared enemies", "Shared entrances") for each row of ``rows``, in order.

    Enemies are written as "752 Gargoyle", and entrances by the neighbor room, as the doors cell
    names them ("917", "104 (left)"). A row without doors or without a requirement gets two empty
    strings. One pass builds two maps, so the cost is linear in the number of door and placement
    pairs.
    """
    parsed = []
    names: dict[int, str] = {}
    placements_by_door: dict[tuple[str, tuple[int, ...]], set[int]] = defaultdict(set)
    doors_by_placement: dict[tuple[int, tuple[int, ...]], set[str]] = defaultdict(set)
    for row in rows:
        number = int(row["enemy_number"])
        names[number] = (row.get("Enemy Name") or "").strip()
        try:
            row_doors = doors(row)
        except ValueError:
            row_doors = set()
        mark = signature(row, ability_headers, combo_headers) if row_doors else None
        parsed.append((number, row_doors, mark))
        if mark is None:
            continue
        for door in row_doors:
            placements_by_door[(door, mark)].add(number)
            doors_by_placement[(number, mark)].add(door)

    out = []
    for number, row_doors, mark in parsed:
        if mark is None:
            out.append(("", ""))
            continue
        enemies = set.intersection(*(placements_by_door[(door, mark)] for door in row_doors))
        enemies.discard(number)
        entrances = doors_by_placement[(number, mark)] - row_doors
        out.append((", ".join(f"{other} {names[other]}" for other in sorted(enemies)),
                    ", ".join(sorted(door.split(":", 1)[1] for door in entrances))))
    return out
