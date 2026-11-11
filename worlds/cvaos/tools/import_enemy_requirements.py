"""
Update the enemy routing requirements from the logic spreadsheet.

The requirements are written in the spreadsheet's enemy tab, "EnemyRequirements".
``data/routing_info/symmetric_entrance_to_enemy_region_requirements.csv`` holds a copy of the
tab's columns from ``enemy_number`` to "Misc. combo 5", plus "Status". This tool downloads the
tab, checks each placement's rows the way the loader will read them, and rewrites the CSV.

A placement whose sheet rows fail a check keeps the rows the CSV already has, and the report says
why. A placement is held back when one of its sheet rows has:

* a requirement (a tick or a combo) but no Status. Rows annotated before the Status column existed
  stay as they are until the sheet gives them one;
* a Status other than Tested or Judged, or a Status but no requirement;
* a requirement but no doors, or doors that name no door of the room or don't say which of two;
* a combo label without a "(Token)", other than "No ability needed", or a token the loader
  doesn't know;
* a TRUE in "<alt 2>", which the loader ignores;
* a door that another of the placement's rows also names.

A placement that has no rows in the sheet also keeps its current rows.

The loader doesn't read Status. Only this tool does, to decide which rows to take.

The tool also warns when the sheet's "Shared enemies" or "Shared entrances" cells differ from what
:mod:`.enemy_sharing` computes. That means someone edited a row by hand since the annotator last
wrote them, and it doesn't hold anything back.

Columns are found by name, so columns can be added after the copied block. The copied block has to
have the same names, in the same order, as the CSV's header.

Run it from the repository root:

    python -m worlds.cvaos.tools.import_enemy_requirements             # rewrite the CSV
    python -m worlds.cvaos.tools.import_enemy_requirements --check     # only report
    python -m worlds.cvaos.tools.import_enemy_requirements --sheet-csv tab.csv
    python -m worlds.cvaos.tools.import_enemy_requirements --sheet-id ID   # e.g. a copy
"""
from __future__ import annotations

import argparse
import csv
import io
import itertools
import os
import re
import urllib.request

from ..data import routing_info
from ..data.routing_info import NO_ABILITY_LABEL, AbilityCombo
from . import enemy_sharing

SHEET_ID = "1FraahXhneKxnXsj_FX9LxrFJLJpsDQJNIrsSCmVWqEo"
ENEMY_TAB_GID = 747853994
EXPORT_URL = "https://docs.google.com/spreadsheets/d/{sheet}/export?format=csv&gid={gid}"
CSV_PATH = os.path.join(os.path.dirname(routing_info.__file__),
                        "symmetric_entrance_to_enemy_region_requirements.csv")
STATUS = "Status"
STATUSES = ("Tested", "Judged")
SHARED_ENEMIES, SHARED_ENTRANCES = "Shared enemies", "Shared entrances"
_TOKEN = re.compile(r"\([^)]+\)")


def download(sheet_id: str = SHEET_ID, gid: int = ENEMY_TAB_GID) -> str:
    with urllib.request.urlopen(EXPORT_URL.format(sheet=sheet_id, gid=gid), timeout=60) as response:
        return response.read().decode("utf-8")


def read_sheet(text: str) -> tuple[list[str], list[dict[str, str]]]:
    """Return the names of the copied block, then the tab's placement rows.

    The block runs from ``enemy_number`` to "Misc. combo 5". Each row is a dict of the block's
    columns, plus Status and the two shared columns when the tab has them.
    """
    rows = list(csv.reader(io.StringIO(text)))
    header_index = next(i for i, row in enumerate(rows)
                        if "enemy_number" in row and "dest_room_identifier" in row)
    header = rows[header_index]
    first, last = header.index("enemy_number"), header.index("Misc. combo 5")
    block = header[first:last + 1]
    extra = {name: header.index(name) for name in (STATUS, SHARED_ENEMIES, SHARED_ENTRANCES)
             if name in header}

    def cell(row: list[str], index: int) -> str:
        return row[index] if index < len(row) else ""

    out = []
    for row in rows[header_index + 1:]:
        if not cell(row, first).strip().isdigit():
            continue
        values = {name: cell(row, first + i) for i, name in enumerate(block)}
        values.update({name: cell(row, index) for name, index in extra.items()})
        out.append(values)
    return block, out


def header_mismatches(block: list[str], header: list[str]) -> list[str]:
    """Return a description of each block column whose name differs from the CSV's header."""
    expected = [name for name in header if name != STATUS]
    return [f"the sheet has {'no column' if name is None else repr(name)} where the CSV has "
            f"{'no column' if wanted is None else repr(wanted)}"
            for name, wanted in itertools.zip_longest(block, expected) if name != wanted]


def problems(row: dict[str, str], ability_headers: list[str], combo_headers: list[str]) -> list[str]:
    """Return the reasons the loader would misread ``row``, or an empty list if there are none."""
    found = []
    number = row["enemy_number"].strip()
    abilities, combos = routing_info._extract_abilities_and_combo_texts(row, ability_headers,
                                                                        combo_headers)
    has_requirement = bool(abilities or combos)
    status = (row.get(STATUS) or "").strip()
    door_cell = (row.get("dest_room_identifier") or "").strip()

    if status and status not in STATUSES:
        found.append(f"enemy {number}: Status {status!r} isn't one of {', '.join(STATUSES)}")
    if has_requirement and not status:
        found.append(f"enemy {number}: has a requirement but no Status")
    if status and not has_requirement:
        found.append(f"enemy {number}: Status is {status} but nothing is ticked")
    if has_requirement and not door_cell:
        found.append(f"enemy {number}: has a requirement but no doors")
    if door_cell:
        try:
            if not enemy_sharing.doors(row):
                found.append(f"enemy {number}: doors {door_cell!r} name no door of room "
                             f"{row.get('room_id')}")
        except ValueError as error:
            found.append(f"enemy {number}: {error}")
    if (row.get("<alt 2>") or "").strip().upper() == "TRUE":
        found.append(f"enemy {number}: <alt 2> is TRUE, which the loader ignores")
    for name in combo_headers:
        text = (row.get(name) or "").strip()
        if not text:
            continue
        untokened = [label.strip() for label in text.split(",") if label.strip()
                     and label.strip() != NO_ABILITY_LABEL and not _TOKEN.search(label)]
        if untokened:
            found.append(f"enemy {number}: {name} has no (Token) for {', '.join(untokened)}")
        try:
            routing_info._parse_combo_text_to_mask(text, strict=True)
        except ValueError as error:
            found.append(f"enemy {number}: {error}")
    return found


def duplicate_doors(rows: list[dict[str, str]]) -> list[str]:
    """Return a reason for each door that more than one of a placement's rows names."""
    seen: dict[str, int] = {}
    found = []
    for row in rows:
        try:
            names = enemy_sharing.doors(row)
        except ValueError:
            continue
        for door in names:
            seen[door] = seen.get(door, 0) + 1
    for door, count in seen.items():
        if count > 1:
            found.append(f"enemy {rows[0]['enemy_number'].strip()}: door {door} is in {count} rows")
    return found


def merge(sheet: list[dict[str, str]], current: list[dict[str, str]], header: list[str],
          ability_headers: list[str], combo_headers: list[str]):
    """Return the merged rows, the placements whose rows the sheet changed, and those held back.

    A placement takes its sheet rows when every row passes :func:`problems` and no door repeats,
    and otherwise keeps its rows from ``current``. Rows come out in the sheet's order. A held-back
    placement's current rows take the place of its first sheet row, and placements the sheet
    doesn't have come last.
    """
    def by_number(rows):
        out: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            out.setdefault(row["enemy_number"].strip(), []).append(row)
        return out

    sheet_by, current_by = by_number(sheet), by_number(current)
    held: dict[str, list[str]] = {}
    for number, rows in sheet_by.items():
        reasons = [reason for row in rows for reason in problems(row, ability_headers, combo_headers)]
        reasons += duplicate_doors(rows)
        if reasons:
            held[number] = reasons

    merged, updated, emitted = [], [], set()
    for row in sheet:
        number = row["enemy_number"].strip()
        if number not in held:
            merged.append(row)
        elif number not in emitted:
            merged.extend(current_by.get(number, []))
        emitted.add(number)
    for number, rows in current_by.items():
        if number not in sheet_by:
            held[number] = [f"enemy {number}: the sheet has no rows for it"]
            merged.extend(rows)
    def cells(rows):
        return [tuple((row.get(name) or "").strip() for name in header) for row in rows]

    for number, rows in sheet_by.items():
        if number not in held and cells(rows) != cells(current_by.get(number, [])):
            updated.append(number)
    return merged, updated, held


def requirements(rows: list[dict[str, str]], ability_headers: list[str],
                 combo_headers: list[str]) -> dict[tuple[str, int], tuple[int, ...]]:
    """Return the ways through for each (door, placement) the rows give a requirement for."""
    out = {}
    for row in rows:
        try:
            names = enemy_sharing.doors(row)
        except ValueError:
            continue
        abilities, combos = routing_info._extract_abilities_and_combo_texts(row, ability_headers,
                                                                            combo_headers)
        if not names or not (abilities or combos):
            continue
        masks = routing_info._requirement_bitmasks_from(abilities, combos,
                                                        strict_combo_tokens=False)
        for door in names:
            out.setdefault((door, int(row["enemy_number"])), masks)
    return out


def describe(masks: tuple[int, ...]) -> str:
    """Return ways through as text, such as "HJump | DJump+Panth", or "free"."""
    if not masks or 0 in masks:
        return "free"
    return " | ".join("+".join(flag.name for flag in AbilityCombo if flag and mask & flag)
                      for mask in masks)


def unrouted_types(requirement_map: dict[tuple[str, int], tuple[int, ...]]) -> list[str]:
    """Return the checkable enemy types with no placement that has routing or an arena rule."""
    from ..enemy_checks import ARENA_PLACEMENTS, checkable_types, placements_of
    routed = {number for _, number in requirement_map}
    return sorted(enemy.name for enemy in checkable_types()
                  if not any(p.enemy_number in routed or p.enemy_number in ARENA_PLACEMENTS
                             for p in placements_of(enemy.name)))


def stale_shared(sheet: list[dict[str, str]], ability_headers: list[str],
                 combo_headers: list[str]) -> list[str]:
    """Return a description of each row whose shared cells differ from the computed ones."""
    if SHARED_ENEMIES not in (sheet[0] if sheet else {}):
        return []
    found = []
    for row, (enemies, entrances) in zip(sheet, enemy_sharing.shared_columns(
            sheet, ability_headers, combo_headers)):
        have = ((row.get(SHARED_ENEMIES) or "").strip(), (row.get(SHARED_ENTRANCES) or "").strip())
        if have != (enemies, entrances):
            found.append(f"enemy {row['enemy_number'].strip()} ({row.get('dest_room_identifier')}): "
                         f"sheet has {have}, computed {(enemies, entrances)}")
    return found


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="report without writing the CSV")
    parser.add_argument("--sheet-csv", help="read the tab from this file instead of downloading it")
    parser.add_argument("--sheet-id", default=SHEET_ID, help="download from this spreadsheet")
    options = parser.parse_args(argv)

    if options.sheet_csv:
        with open(options.sheet_csv, encoding="utf-8", newline="") as f:
            text = f.read()
    else:
        text = download(options.sheet_id)
    with open(CSV_PATH, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames or [])
        current = [row for row in reader if (row.get("enemy_number") or "").strip().isdigit()]
    block, sheet = read_sheet(text)
    differences = header_mismatches(block, header)
    if differences:
        raise SystemExit("The sheet's column names differ from the CSV's header, so the CSV is "
                         "unchanged:\n" + "\n".join(f"    {line}" for line in differences))
    header = block + [STATUS]
    ability_headers, combo_headers = routing_info._ability_and_combo_headers(header)
    merged, updated, held = merge(sheet, current, header, ability_headers, combo_headers)

    print(f"{len(updated)} placements updated from the sheet: {', '.join(updated) or '-'}")
    for number, reasons in held.items():
        print(f"enemy {number} keeps its current rows:")
        for reason in dict.fromkeys(reasons):
            print(f"    {reason}")

    before = requirements(current, ability_headers, combo_headers)
    after = requirements(merged, ability_headers, combo_headers)
    changes = [(key, before.get(key), after.get(key)) for key in sorted(set(before) | set(after))
               if before.get(key) != after.get(key)]
    print(f"{len(changes)} door and placement requirements change:")
    for (door, number), old, new in changes:
        print(f"    {door} -> enemy {number}: "
              f"{'none' if old is None else describe(old)} => {'none' if new is None else describe(new)}")
    unrouted_before, unrouted_after = unrouted_types(before), unrouted_types(after)
    print(f"unrouted enemy types: {len(unrouted_before)} before, {len(unrouted_after)} after")

    stale = stale_shared(sheet, ability_headers, combo_headers)
    if stale:
        print(f"warning: {len(stale)} rows have out-of-date shared columns, for example:")
        for line in stale[:5]:
            print(f"    {line}")

    if options.check:
        return
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows([row.get(name, "") for name in header] for row in merged)
    print(f"wrote {os.path.relpath(CSV_PATH)}")


if __name__ == "__main__":
    main()
