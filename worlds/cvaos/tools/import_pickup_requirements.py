"""
Update the pickup requirements from the logic spreadsheet.

The spreadsheet's pickup tab is where the requirements are written, and
``data/routing_info/symmetric_entrance_to_pickup_region_requirements.csv`` is a copy of its
columns from ``pickup_number`` to the last "Misc. combo" column. This tool downloads the tab,
checks each pickup's rows the way the loader will read them, and rewrites the CSV.

A pickup whose sheet rows fail a check keeps the rows the CSV already has, and the report says
why. These rows are held back:

* a ``dest_room_identifier`` that names no door of the room, or names a room pair with two
  doors without saying which;
* a combo with a part that has no ``(Token)``, or a token the loader doesn't know;
* a TRUE in ``<alt 2>``, which the loader ignores;
* a blank variant column.

The sheet sometimes holds a sixth combo in the unnamed column after "Misc. combo 5". It is kept
as "Misc. combo 6", which the loader reads like the others. The columns after that hold notes and
are not copied.

The columns are copied by position, so the tool first checks that the sheet's names for them
match the CSV's header. The sixth combo's blank name matches "Misc. combo 6". When a name differs,
the tool lists each difference and stops without changing the CSV.

Run it from the repository root:

    python -m worlds.cvaos.tools.import_pickup_requirements           # rewrite the CSV
    python -m worlds.cvaos.tools.import_pickup_requirements --check   # only report
    python -m worlds.cvaos.tools.import_pickup_requirements --sheet-csv tab.csv
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

SHEET_ID = "1FraahXhneKxnXsj_FX9LxrFJLJpsDQJNIrsSCmVWqEo"
PICKUP_TAB_GID = 1829168067
EXPORT_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={{gid}}"
CSV_PATH = os.path.join(os.path.dirname(routing_info.__file__),
                        "symmetric_entrance_to_pickup_region_requirements.csv")
EXTRA_COMBO = "Misc. combo 6"
_TOKEN = re.compile(r"\([^)]+\)")


def download(gid: int = PICKUP_TAB_GID) -> str:
    with urllib.request.urlopen(EXPORT_URL.format(gid=gid), timeout=60) as response:
        return response.read().decode("utf-8")


def read_sheet(text: str) -> tuple[list[str], list[list[str]]]:
    """Return the sheet's names for the copied columns, then the tab's pickup rows.

    The copied columns are the CSV's columns plus the sixth combo, in order, and each row holds
    only those columns.
    """
    rows = list(csv.reader(io.StringIO(text)))
    header_index = next(i for i, row in enumerate(rows) if "pickup_number" in row)
    header = rows[header_index]
    first = header.index("pickup_number")
    destination = header.index("dest_room_identifier")
    abilities = header.index("None")
    last_combo = header.index("Misc. combo 5")
    columns = ([first, first + 1, first + 2, destination - 1, destination]
               + list(range(abilities, last_combo + 2)))
    names = [header[i] if i < len(header) else "" for i in columns]
    return names, [[row[i] if i < len(row) else "" for i in columns]
                   for row in rows[header_index + 1:]
                   if len(row) > first and row[first].strip().isdigit()]


def header_mismatches(names: list[str], header: list[str]) -> list[str]:
    """Return a description of each copied column whose sheet name differs from the CSV's header.

    ``names`` ends with the sheet's sixth combo column, whose name can be blank or
    "Misc. combo 6". The CSV's ``header`` can leave that column out, because main adds it when the
    sheet uses it. Returns an empty list when the names match.
    """
    expected = [name for name in header if name != EXTRA_COMBO]
    differences = [f"the sheet has {_shown(name)} where the CSV has {_shown(wanted)}"
                   for name, wanted in itertools.zip_longest(names[:-1], expected)
                   if name != wanted]
    if names[-1] not in ("", EXTRA_COMBO):
        differences.append(f"the sheet has {names[-1]!r} where the CSV has {EXTRA_COMBO!r}")
    return differences


def _shown(name: str | None) -> str:
    """Return ``name`` quoted, or "no column" when one side has no column there."""
    return "no column" if name is None else repr(name)


def problems(row: dict) -> list[str]:
    """Return why the loader would misread ``row``, or nothing when it reads it correctly."""
    found = []
    if not row[""].strip():
        found.append("the variant column is blank")
    try:
        entrances = routing_info._entrance_identifiers_from_cell(row["Room"],
                                                                 row["dest_room_identifier"])
        if not entrances:
            found.append(f"dest_room_identifier {row['dest_room_identifier']!r} names no door of "
                         f"room {row['Room']}")
    except ValueError as error:
        found.append(str(error))
    if row.get("<alt 2>", "").strip().upper() == "TRUE":
        found.append("<alt 2> is TRUE, which the loader ignores")
    for name, text in row.items():
        if not name.startswith("Misc. combo") or not text.strip():
            continue
        untokened = [part.strip() for part in text.split(",") if part.strip()
                     and not _TOKEN.search(part)]
        if untokened:
            found.append(f"{name} has no (Token) for {', '.join(untokened)}: {text!r}")
        try:
            routing_info._parse_combo_text_to_mask(text, strict=True)
        except ValueError as error:
            found.append(str(error))
    return found


def merge(sheet: list[list[str]], current: list[list[str]], header: list[str]):
    """Return the merged rows, and the pickups taken from the sheet and those held back."""
    def by_pickup(rows):
        out: dict[int, list[list[str]]] = {}
        for row in rows:
            out.setdefault(int(row[0]), []).append(row)
        return out

    width = len(header)
    sheet_by_pickup = by_pickup(row[:width] + [""] * (width - len(row)) for row in sheet)
    current_by_pickup = by_pickup(row + [""] * (width - len(row)) for row in current)
    merged, updated, held = [], [], {}
    for number in sorted(set(sheet_by_pickup) | set(current_by_pickup)):
        rows = sheet_by_pickup.get(number)
        reasons = [reason for row in rows or () for reason in problems(dict(zip(header, row)))]
        if rows is None:
            held[number] = ["the sheet has no rows for it"]
        elif reasons:
            held[number] = reasons
        if rows is not None and not reasons:
            if rows != current_by_pickup.get(number):
                updated.append(number)
            merged.extend(rows)
        else:
            merged.extend(current_by_pickup.get(number, []))
    return merged, updated, held


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="report without writing the CSV")
    parser.add_argument("--sheet-csv", help="read the tab from this file instead of downloading it")
    options = parser.parse_args(argv)

    if options.sheet_csv:
        with open(options.sheet_csv, encoding="utf-8", newline="") as f:
            text = f.read()
    else:
        text = download()
    with open(CSV_PATH, encoding="utf-8", newline="") as f:
        current = list(csv.reader(f))
    header = current[0]
    names, sheet = read_sheet(text)
    differences = header_mismatches(names, header)
    if differences:
        raise SystemExit("The sheet's column names differ from the CSV's header, so the CSV is "
                         "unchanged:\n" + "\n".join(f"    {line}" for line in differences))
    if EXTRA_COMBO not in header and any(row[-1].strip() for row in sheet):
        header = header + [EXTRA_COMBO]
    merged, updated, held = merge(sheet, [row for row in current[1:] if any(row)], header)

    print(f"{len(updated)} pickups updated from the sheet: {', '.join(map(str, updated)) or '-'}")
    for number, reasons in held.items():
        print(f"pickup {number} keeps its current rows:")
        for reason in dict.fromkeys(reasons):
            print(f"    {reason}")
    if options.check:
        return
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        csv.writer(f, lineterminator="\n").writerows([header, *merged])
    print(f"wrote {os.path.relpath(CSV_PATH)}")


if __name__ == "__main__":
    main()
