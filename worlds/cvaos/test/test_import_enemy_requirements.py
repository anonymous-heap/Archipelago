"""
The enemy requirements importer takes a placement's rows from the sheet only when the loader would
read them as meant, and the loader reads "No ability needed" as free and an empty row as not done.

    python -m pytest worlds/cvaos/test/test_import_enemy_requirements.py -v
"""
from __future__ import annotations

import contextlib
import csv
import io
import os
import tempfile
import unittest
from unittest import mock

from ..data import routing_info
from ..data.routing_info import AbilityCombo
from ..tools import enemy_sharing
from ..tools import import_enemy_requirements as importer

BLOCK = ["enemy_number", "room_id", "Enemy Name", "Specifier", "x", "-y", "Prog.?",
         "dest_room_identifier", "None", "Glide", "Slide", "DJump", "HJump", "WWalk", "Dive",
         "Panth.", "Bat", "Impossible", "Kick", "<alt 2>", "Misc. combo 1", "Misc. combo 2",
         "Misc. combo 3", "Misc. combo 4", "Misc. combo 5"]
ABILITIES, COMBOS = routing_info._ability_and_combo_headers(BLOCK + [importer.STATUS])
DJUMP, HJUMP, BAT = int(AbilityCombo.DJump), int(AbilityCombo.HJump), int(AbilityCombo.Bat)


def row(number: int, name: str, doors: str = "", ticks: tuple[str, ...] = (),
        combos: tuple[str, ...] = (), status: str = "", room: str = "90D") -> dict[str, str]:
    """Return an enemy tab row in room 90D (doors 917, 90C and 90E) as a dict of columns."""
    values = {name_: "" for name_ in BLOCK}
    values.update({"enemy_number": str(number), "room_id": room, "Enemy Name": name,
                   "dest_room_identifier": doors, importer.STATUS: status})
    for ability in ABILITIES:
        values[ability] = "TRUE" if routing_info._canonical_ability_name(ability) in ticks else "FALSE"
    for header, text in zip(COMBOS, combos):
        values[header] = text
    return values


def sheet_text(rows: list[dict[str, str]], shared: bool = False) -> str:
    """Return an enemy tab laid out like the live sheet: tracker columns first, Status last."""
    extra = [importer.STATUS] + ([importer.SHARED_ENEMIES, importer.SHARED_ENTRANCES] if shared else [])
    header = ["", "41", "/", *BLOCK, "Req.", *extra]
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    for values in rows:
        writer.writerow(["", "", "", *(values[name] for name in BLOCK), "",
                         *(values.get(name, "") for name in extra)])
    return out.getvalue()


class ComboParsingTest(unittest.TestCase):
    def test_no_ability_needed_is_a_free_way_through(self):
        self.assertEqual(routing_info._parse_combo_text_to_mask("No ability needed", strict=True), 0)
        self.assertEqual(routing_info._requirement_bitmasks_from(
            frozenset({"DJump"}), ("No ability needed",)), (0,))

    def test_no_ability_needed_beside_a_token_adds_nothing(self):
        self.assertEqual(routing_info._parse_combo_text_to_mask(
            "No ability needed, Malphas (DJump)", strict=True), DJUMP)

    def test_a_label_without_a_token_still_reads_as_nothing(self):
        self.assertIsNone(routing_info._parse_combo_text_to_mask(
            "Reset Luck/Very Painful Grinding", strict=True))


class LoaderTest(unittest.TestCase):
    def load(self, rows: list[dict[str, str]]):
        text = io.StringIO()
        writer = csv.DictWriter(text, fieldnames=BLOCK, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        reader = csv.DictReader(io.StringIO(text.getvalue()))
        with mock.patch.object(routing_info, "open_csv", return_value=reader):
            return routing_info._load_enemy_region_requirements()

    def test_a_row_with_doors_but_no_requirement_is_not_done(self):
        self.assertEqual(self.load([row(751, "Succubus", doors="917")]), ())

    def test_a_door_named_twice_for_one_placement_is_an_error(self):
        rows = [row(751, "Succubus", doors="917", ticks=("DJump",)),
                row(751, "Succubus", doors="917, 90C", ticks=("Bat",))]
        with self.assertRaisesRegex(ValueError, "more than one row for door 90D:917"):
            self.load(rows)


class ProblemsTest(unittest.TestCase):
    def reasons(self, values: dict[str, str]) -> str:
        return "\n".join(importer.problems(values, ABILITIES, COMBOS))

    def test_a_finished_row_passes(self):
        self.assertEqual(self.reasons(row(751, "Succubus", doors="917", ticks=("DJump", "Bat"),
                                          status="Tested")), "")

    def test_a_requirement_needs_a_status(self):
        self.assertIn("no Status", self.reasons(row(751, "Succubus", doors="917", ticks=("DJump",))))

    def test_a_status_needs_a_requirement(self):
        self.assertIn("nothing is ticked", self.reasons(row(751, "Succubus", doors="917",
                                                            status="Judged")))

    def test_an_unknown_status_is_named(self):
        self.assertIn("isn't one of", self.reasons(row(751, "Succubus", doors="917",
                                                       ticks=("Bat",), status="Done")))

    def test_a_requirement_needs_doors(self):
        self.assertIn("no doors", self.reasons(row(751, "Succubus", ticks=("Bat",), status="Tested")))

    def test_a_label_without_a_token_is_named(self):
        self.assertIn("no (Token) for Reset Luck", self.reasons(row(
            751, "Succubus", doors="917", combos=("Reset Luck/Very Painful Grinding",),
            status="Judged")))

    def test_no_ability_needed_needs_no_token(self):
        self.assertEqual(self.reasons(row(751, "Succubus", doors="917",
                                          combos=("No ability needed",), status="Judged")), "")

    def test_duplicate_doors_are_named(self):
        rows = [row(751, "Succubus", doors="917", ticks=("DJump",), status="Tested"),
                row(751, "Succubus", doors="917, 90C", ticks=("Bat",), status="Tested")]
        self.assertEqual(importer.duplicate_doors(rows), ["enemy 751: door 90D:917 is in 2 rows"])


class SharingTest(unittest.TestCase):
    def test_placements_and_doors_with_the_same_requirement_are_listed(self):
        rows = [row(751, "Succubus", doors="917", ticks=("DJump", "Bat"), status="Tested"),
                # The same requirement, written as combos in another order.
                row(752, "Gargoyle", doors="917", combos=("Giant Bat (Bat)", "Malphas (DJump)"),
                    status="Judged"),
                row(751, "Succubus", doors="90C", ticks=("DJump", "Bat"), status="Tested"),
                row(753, "Succubus", doors="917", ticks=("HJump",), status="Tested"),
                row(754, "Gargoyle")]
        self.assertEqual(enemy_sharing.shared_columns(rows, ABILITIES, COMBOS), [
            ("752 Gargoyle", "90C"), ("751 Succubus", ""), ("", "917"), ("", ""), ("", "")])

    def test_a_shared_enemy_must_match_from_every_door_of_the_row(self):
        rows = [row(751, "Succubus", doors="917, 90C", ticks=("Bat",), status="Tested"),
                row(752, "Gargoyle", doors="917", ticks=("Bat",), status="Tested")]
        self.assertEqual(enemy_sharing.shared_columns(rows, ABILITIES, COMBOS)[0], ("", ""))

    def test_signature_ignores_how_a_requirement_is_written(self):
        ticked = row(751, "Succubus", doors="917", ticks=("DJump",))
        combo = row(751, "Succubus", doors="917", combos=("Malphas (DJump)",))
        self.assertEqual(enemy_sharing.signature(ticked, ABILITIES, COMBOS),
                         enemy_sharing.signature(combo, ABILITIES, COMBOS))


class ImportTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.sheet_path = os.path.join(folder.name, "tab.csv")
        self.csv_path = os.path.join(folder.name, "requirements.csv")
        # The current CSV: 751 is free from 917, 752 isn't annotated, plus a filler row.
        current = [row(751, "Succubus", doors="917", ticks=("None",)), row(752, "Gargoyle")]
        with open(self.csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(BLOCK)
            writer.writerows([[values[name] for name in BLOCK] for values in current])
            writer.writerow([""] * 8 + ["FALSE"] * 12 + [""] * 5)
        patcher = mock.patch.object(importer, "CSV_PATH", self.csv_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_tool(self, rows: list[dict[str, str]], *args: str) -> str:
        with open(self.sheet_path, "w", encoding="utf-8", newline="") as f:
            f.write(sheet_text(rows))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            importer.main(["--sheet-csv", self.sheet_path, *args])
        return output.getvalue()

    def written(self) -> list[dict[str, str]]:
        with open(self.csv_path, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    def test_a_placement_without_status_keeps_its_current_rows(self):
        report = self.run_tool([row(751, "Succubus", doors="917", ticks=("Bat",)),
                                row(752, "Gargoyle", doors="917", ticks=("HJump",),
                                    status="Tested")])
        self.assertIn("enemy 751 keeps its current rows", report)
        self.assertIn("1 placements updated from the sheet: 752", report)
        self.assertIn("90D:917 -> enemy 752: none => HJump", report)
        rows = self.written()
        self.assertEqual(list(rows[0]), BLOCK + [importer.STATUS])
        self.assertEqual([(r["enemy_number"], r["None"], r["Bat"], r["HJump"], r["Status"])
                          for r in rows],
                         [("751", "TRUE", "FALSE", "FALSE", ""), ("752", "FALSE", "FALSE", "TRUE",
                                                                  "Tested")])

    def test_check_writes_nothing(self):
        with open(self.csv_path, encoding="utf-8") as f:
            before = f.read()
        self.run_tool([row(751, "Succubus", doors="917", ticks=("Bat",), status="Tested"),
                       row(752, "Gargoyle")], "--check")
        with open(self.csv_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)

    def test_different_column_names_stop_the_import(self):
        with open(self.sheet_path, "w", encoding="utf-8", newline="") as f:
            f.write(sheet_text([row(751, "Succubus")]).replace("Misc. combo 2", "Combo 2", 1))
        with self.assertRaises(SystemExit) as raised:
            importer.main(["--sheet-csv", self.sheet_path])
        self.assertIn("'Combo 2' where the CSV has 'Misc. combo 2'", str(raised.exception))

    def test_out_of_date_shared_columns_are_reported(self):
        rows = [row(751, "Succubus", doors="917", ticks=("Bat",), status="Tested"),
                row(752, "Gargoyle", doors="917", ticks=("Bat",), status="Tested")]
        with open(self.sheet_path, "w", encoding="utf-8", newline="") as f:
            f.write(sheet_text(rows, shared=True))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            importer.main(["--sheet-csv", self.sheet_path, "--check"])
        self.assertIn("2 rows have out-of-date shared columns", output.getvalue())


if __name__ == "__main__":
    unittest.main()
