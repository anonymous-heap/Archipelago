"""
The logic sheet's enemy tab, and the edits the annotator makes to it.

A row of the tab names one placement (an enemy in a room, by ``enemy_number``) and a set of doors
(``dest_room_identifier``), then gives what those doors need to reach the placement: ticked
ability columns, and combo cells whose labels together make one way through. :class:`EnemyTab`
reads the tab once, and each action returns an :class:`Edit`, the cell changes that carry it out.

Rows are always found by enemy number and doors cell, never by a remembered row number, because
someone may insert rows by hand while the annotator is open. The server checks that a placement's
rows still read the way they did when the page saw them before it writes (see
:meth:`EnemyTab.placement_state`).
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ...data import routing_info
from .. import enemy_sharing
from ..import_enemy_requirements import SHARED_ENEMIES, SHARED_ENTRANCES, STATUS, STATUSES
from .sheets import column_letter

TAB_TITLE = "EnemyRequirements"
NOTES, ANNOTATED_BY, DATE, VERIFIED_BY = "Notes", "Annotated by", "Date", "Verified by"
# The columns that say which placement a row is about, copied when a new row is added for it.
IDENTITY = ("enemy_number", "room_id", "Enemy Name", "Specifier", "x", "-y", "Prog.?")
DOORS = "dest_room_identifier"


@dataclass
class Row:
    """One placement row: its 1-based row in the sheet, and its cells by column name."""
    number: int
    values: dict[str, str]

    @property
    def enemy(self) -> int:
        return int(self.values["enemy_number"])

    @property
    def doors_cell(self) -> str:
        return (self.values.get(DOORS) or "").strip()


@dataclass
class Requirement:
    """What a row's doors need, as the editor shows it.

    ``ticks`` holds ability column names as the header spells them ("Panth." included), and
    ``combos`` holds up to five combo cells, each a comma-separated list of dropdown labels.
    """
    ticks: frozenset[str] = frozenset()
    combos: tuple[str, ...] = ()
    status: str = ""
    notes: str = ""


@dataclass
class Edit:
    """Cell changes for one action.

    ``writes`` use RAW input, ``dated`` use USER_ENTERED (so the sheet stores a date), and
    ``clears`` empty cells. ``insert_after`` asks for a new row below each listed row first, in
    which case the server reads the tab again and repeats the action.
    """
    writes: list[tuple[str, list[list]]] = field(default_factory=list)
    dated: list[tuple[str, list[list]]] = field(default_factory=list)
    clears: list[str] = field(default_factory=list)
    insert_after: list[int] = field(default_factory=list)
    # The enemy number each written row belongs to, so the server can check after writing that
    # no row moved underneath the edit.
    touched: dict[int, str] = field(default_factory=dict)

    def extend(self, other: "Edit") -> None:
        self.writes += other.writes
        self.dated += other.dated
        self.clears += other.clears
        self.insert_after += other.insert_after
        self.touched.update(other.touched)


class EditError(ValueError):
    """An action that can't be carried out as asked, with the reason for the page."""


class EnemyTab:
    """The enemy tab as read from the sheet: its columns and its placement rows."""

    def __init__(self, grid: list[list[str]]):
        header_index = next(i for i, row in enumerate(grid)
                            if "enemy_number" in row and DOORS in row)
        header = grid[header_index]
        first, last = header.index("enemy_number"), header.index("Misc. combo 5")
        self.block = header[first:last + 1]
        self.column: dict[str, int] = {name: first + i for i, name in enumerate(self.block)}
        for name in (STATUS, SHARED_ENEMIES, SHARED_ENTRANCES, NOTES, ANNOTATED_BY, DATE,
                     VERIFIED_BY):
            if name in header:
                self.column[name] = header.index(name)
        self.abilities, self.combos = routing_info._ability_and_combo_headers(self.block + [STATUS])
        self.rows: list[Row] = []
        for offset, cells in enumerate(grid[header_index + 1:], start=header_index + 2):
            values = {name: (cells[index] if index < len(cells) else "")
                      for name, index in self.column.items()}
            if values["enemy_number"].strip().isdigit():
                self.rows.append(Row(offset, values))

    # --- reading -------------------------------------------------------------------------------

    def rows_of(self, enemy: int) -> list[Row]:
        return [row for row in self.rows if row.enemy == enemy]

    def row(self, enemy: int, doors_cell: str) -> Row:
        for row in self.rows_of(enemy):
            if row.doors_cell == doors_cell.strip():
                return row
        raise EditError(f"enemy {enemy} no longer has a row with doors "
                        f"{doors_cell.strip() or '(none)'}, so it may have changed in the sheet. "
                        f"Pick the row again.")

    def requirement(self, row: Row) -> Requirement:
        values = row.values
        ticks = frozenset(name for name in self.abilities
                          if (values.get(name) or "").strip().upper() == "TRUE")
        combos = tuple((values.get(name) or "").strip() for name in self.combos
                       if (values.get(name) or "").strip())
        return Requirement(ticks, combos, (values.get(STATUS) or "").strip(),
                           (values.get(NOTES) or "").strip())

    def doors(self, row: Row) -> set[str]:
        """Return the entrance nodes the row names, such as "90D:917".

        The set is empty when the cell can't be read.
        """
        try:
            return enemy_sharing.doors(row.values)
        except ValueError:
            return set()

    def masks(self, row: Row) -> Optional[tuple[int, ...]]:
        """Return the row's ways through as the loader reads them, or None when it has none."""
        requirement = self.requirement(row)
        if not requirement.ticks and not requirement.combos:
            return None
        abilities = frozenset(routing_info._canonical_ability_name(t) for t in requirement.ticks)
        return routing_info._requirement_bitmasks_from(abilities, requirement.combos,
                                                       strict_combo_tokens=False)

    def pair_requirements(self) -> dict[tuple[str, int], tuple[int, ...]]:
        """Return the ways through for each (door, placement) that a row gives a requirement for."""
        out = {}
        for row in self.rows:
            masks = self.masks(row)
            if masks is None:
                continue
            for door in self.doors(row):
                out.setdefault((door, row.enemy), masks)
        return out

    def blank_row(self, enemy: int) -> Optional[Row]:
        """Return the placement's first row with no doors and no requirement, if it has one."""
        return next((row for row in self.rows_of(enemy)
                     if not row.doors_cell and self.masks(row) is None), None)

    def placement_state(self, enemy: int) -> list[tuple]:
        """Return a placement's rows as plain values, for checking that nobody changed them.

        Row numbers are left out, so rows inserted or deleted elsewhere in the tab don't count as
        a change to this placement.
        """
        return [tuple(sorted(row.values.items())) for row in self.rows_of(enemy)]

    def shared(self) -> list[tuple[str, str]]:
        return enemy_sharing.shared_columns([row.values for row in self.rows], self.abilities,
                                            self.combos)

    # --- editing -------------------------------------------------------------------------------

    def _cell(self, name: str, row: Row) -> str:
        if name not in self.column:
            raise EditError(f"the tab has no {name!r} column")
        return f"'{TAB_TITLE}'!{column_letter(self.column[name])}{row.number}"

    def _set(self, edit: Edit, row: Row, name: str, value) -> None:
        """Write ``value`` into the row's ``name`` cell, or clear it when ``value`` is "" or None.

        A cell that already holds the value is left alone, so a save only touches the cells it
        changes, and the sheet's version history shows only those. An empty checkbox cell counts
        as unticked.
        """
        current = (row.values.get(name) or "").strip()
        if isinstance(value, bool):
            unchanged = (current.upper() == "TRUE") == value
            shown = "TRUE" if value else "FALSE"
        elif value in ("", None):
            unchanged = current == ""
            shown = ""
        else:
            unchanged = current == str(value)
            shown = str(value)
        if unchanged:
            return
        edit.touched[row.number] = (row.values.get("enemy_number") or "").strip() or (
            str(value) if name == "enemy_number" else "")
        if shown == "":
            edit.clears.append(self._cell(name, row))
        else:
            edit.writes.append((self._cell(name, row), [[value]]))
        row.values[name] = shown

    def _write_requirement(self, row: Row, requirement: Requirement, annotator: str,
                           today: Optional[datetime.date]) -> Edit:
        unknown = set(requirement.ticks) - set(self.abilities)
        if unknown:
            raise EditError(f"unknown ability columns {sorted(unknown)}")
        if len(requirement.combos) > len(self.combos):
            raise EditError(f"at most {len(self.combos)} combos fit in a row")
        if requirement.status and requirement.status not in STATUSES:
            raise EditError(f"Status must be one of {', '.join(STATUSES)}")
        edit = Edit()
        for name in self.abilities:
            if name != "<alt 2>":
                self._set(edit, row, name, name in requirement.ticks)
        for index, name in enumerate(self.combos):
            self._set(edit, row, name, requirement.combos[index] if index < len(requirement.combos)
                      else "")
        self._set(edit, row, STATUS, requirement.status)
        if NOTES in self.column:
            self._set(edit, row, NOTES, requirement.notes)
        if annotator and ANNOTATED_BY in self.column:
            self._set(edit, row, ANNOTATED_BY, annotator)
        if today and DATE in self.column:
            edit.dated.append((self._cell(DATE, row), [[today.isoformat()]]))
            edit.touched[row.number] = row.values["enemy_number"].strip()
            row.values[DATE] = today.isoformat()
        return edit

    def record(self, enemy: int, doors_cell: str, requirement: Requirement, *, annotator: str = "",
               today: Optional[datetime.date] = None, new_doors_cell: Optional[str] = None) -> Edit:
        """Set a row's requirement, and optionally its doors, then refresh the shared columns."""
        row = self.row(enemy, doors_cell)
        edit = Edit()
        if new_doors_cell is not None and new_doors_cell.strip() != row.doors_cell:
            self._check_doors(row, new_doors_cell)
            self._set(edit, row, DOORS, new_doors_cell.strip())
        edit.extend(self._write_requirement(row, requirement, annotator, today))
        edit.extend(self._shared_edit())
        return edit

    def same_for_enemies(self, enemy: int, doors_cell: str, targets: Iterable[int], *,
                         annotator: str = "", today: Optional[datetime.date] = None,
                         overwrite: bool = False) -> Edit:
        """Give each target placement a row with the same doors and requirement as this row.

        A target uses its row with the same doors cell when it has one (only with ``overwrite``
        if that row already has a requirement), otherwise its first blank row. A target with
        neither gets a new row below its last one: the edit then only asks for that insert.
        """
        source = self.row(enemy, doors_cell)
        requirement = self.requirement(source)
        edit = Edit()
        for target in targets:
            rows = self.rows_of(target)
            if not rows:
                raise EditError(f"no rows for enemy {target}")
            if rows[0].values["room_id"].strip() != source.values["room_id"].strip():
                raise EditError(f"enemy {target} isn't in room {source.values['room_id']}")
            same = next((row for row in rows if row.doors_cell == source.doors_cell), None)
            # Checked before any row is inserted, so a refusal leaves no empty row behind.
            covered = {door for row in rows if row is not same for door in self.doors(row)}
            if covered & self.doors(source):
                raise EditError(f"enemy {target} already has a row naming "
                                f"{', '.join(sorted(covered & self.doors(source)))}")
            if same is not None and self.masks(same) is not None and not overwrite:
                raise EditError(f"enemy {target} already has a requirement from "
                                f"{source.doors_cell}; confirm to overwrite it")
            target_row = same or next((row for row in rows if not row.doors_cell
                                       and self.masks(row) is None), None)
            if target_row is None:
                edit.insert_after.append(rows[-1].number)
                continue
            if target_row is not same:
                self._check_doors(target_row, source.doors_cell)
                self._set(edit, target_row, DOORS, source.doors_cell)
            edit.extend(self._write_requirement(target_row, requirement, annotator, today))
        if edit.insert_after:
            return Edit(insert_after=edit.insert_after)
        edit.extend(self._shared_edit())
        return edit

    def fill_new_row(self, enemy: int, row_number: int) -> Edit:
        """Copy a placement's identity columns into the blank row inserted at ``row_number``."""
        source = self.rows_of(enemy)[0] if self.rows_of(enemy) else None
        if source is None:
            raise EditError(f"no rows for enemy {enemy}")
        row = Row(row_number, {name: "" for name in self.column})
        edit = Edit()
        for name in IDENTITY:
            if name in self.column and source.values.get(name, "").strip():
                value = source.values[name].strip()
                self._set(edit, row, name, int(value) if name == "enemy_number" else value)
        return edit

    def same_for_entrances(self, enemy: int, doors_cell: str, doors: Iterable[str]) -> Edit:
        """Add doors, by neighbor room as the doors cell names them, to the row's doors cell."""
        row = self.row(enemy, doors_cell)
        entries = [part.strip() for part in row.doors_cell.split(",") if part.strip()]
        if "Any" in entries:
            raise EditError("the row already covers every door (Any)")
        added = [door for door in doors if door not in entries]
        new_cell = ",".join(entries + added)
        self._check_doors(row, new_cell)
        edit = Edit()
        self._set(edit, row, DOORS, new_cell)
        edit.extend(self._shared_edit())
        return edit

    def _check_doors(self, row: Row, doors_cell: str) -> None:
        """Raise when ``doors_cell`` names no door, or a door another row of the placement names."""
        probe = dict(row.values, **{DOORS: doors_cell})
        try:
            wanted = enemy_sharing.doors(probe)
        except ValueError as error:
            raise EditError(str(error)) from error
        if not wanted:
            raise EditError(f"{doors_cell!r} names no door of room {row.values['room_id']}")
        for other in self.rows_of(row.enemy):
            if other.number != row.number and wanted & self.doors(other):
                raise EditError(f"row {other.number} already names "
                                f"{', '.join(sorted(wanted & self.doors(other)))}")

    def _shared_edit(self) -> Edit:
        """Return the shared-column changes that the rows' current values call for."""
        edit = Edit()
        if SHARED_ENEMIES not in self.column or SHARED_ENTRANCES not in self.column:
            return edit
        for row, (enemies, entrances) in zip(self.rows, self.shared()):
            if (row.values.get(SHARED_ENEMIES) or "").strip() != enemies:
                self._set(edit, row, SHARED_ENEMIES, enemies)
            if (row.values.get(SHARED_ENTRANCES) or "").strip() != entrances:
                self._set(edit, row, SHARED_ENTRANCES, entrances)
        return edit
