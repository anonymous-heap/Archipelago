"""
The pickup requirements importer copies the sheet's columns by position, so it stops without
changing the CSV when the sheet's column names differ from the CSV's header.

    python -m pytest worlds/cvaos/test/test_import_pickup_requirements.py -v
"""
from __future__ import annotations

import contextlib
import csv
import io
import os
import tempfile
import unittest
from unittest import mock

from ..tools import import_pickup_requirements as importer

# The columns from "None" to "Misc. combo 5", which the sheet and the CSV name alike.
REQUIREMENTS = ["None", "Glide", "Slide", "DJump", "HJump", "WWalk", "Dive", "Panth.", "Bat",
                "Impossible", "Kick", "<alt 2>", "Misc. combo 1", "Misc. combo 2",
                "Misc. combo 3", "Misc. combo 4", "Misc. combo 5"]
CSV_HEADER = ["pickup_number", "Room", "Item Name", "", "dest_room_identifier", *REQUIREMENTS,
              "Misc. combo 6"]
# The Lucky Charm needs Malphas (DJump), Hippogryph (HJump) or Giant Bat (Bat). The CSV that the
# tool updates starts with an older row that needs only Malphas.
LUCKY_CHARM = ["1", "000", "Lucky Charm", "1", "Any", "FALSE", "FALSE", "FALSE", "TRUE", "TRUE",
               "FALSE", "FALSE", "FALSE", "TRUE", "FALSE", "FALSE", "FALSE", "", "", "", "", "", ""]
OLD_LUCKY_CHARM = ["1", "000", "Lucky Charm", "1", "Any", "FALSE", "FALSE", "FALSE", "TRUE",
                   "FALSE", "FALSE", "FALSE", "FALSE", "FALSE", "FALSE", "FALSE", "FALSE", "", "",
                   "", "", "", ""]


def sheet_text(requirements: list[str], row: list[str]) -> str:
    """Return a pickup tab laid out like the live sheet, with one pickup row.

    ``requirements`` names the columns from "None" to "Misc. combo 5". ``row`` holds the CSV's
    columns plus the sixth combo. Like the live tab, this one has a notes column first, a "Suffix"
    column and a blank column before the variant column, and a "Req." column last.
    """
    header = ["Notes", "pickup_number", "Room", "Item Name", "Suffix", "", "",
              "dest_room_identifier", *requirements, "", "Req."]
    values = ["", *row[:3], "", "", *row[3:], ""]
    text = io.StringIO()
    csv.writer(text).writerows([header, values])
    return text.getvalue()


class HeaderCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.sheet_path = os.path.join(folder.name, "tab.csv")
        self.csv_path = os.path.join(folder.name, "requirements.csv")
        with open(self.csv_path, "w", encoding="utf-8", newline="") as f:
            csv.writer(f, lineterminator="\n").writerows([CSV_HEADER, OLD_LUCKY_CHARM])
        patcher = mock.patch.object(importer, "CSV_PATH", self.csv_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_tool(self, requirements: list[str]) -> str:
        """Run the tool on a tab whose requirement columns have the names ``requirements``.

        Returns what the tool printed.
        """
        with open(self.sheet_path, "w", encoding="utf-8", newline="") as f:
            f.write(sheet_text(requirements, LUCKY_CHARM))
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            importer.main(["--sheet-csv", self.sheet_path])
        return printed.getvalue()

    def csv_rows(self) -> list[list[str]]:
        with open(self.csv_path, encoding="utf-8", newline="") as f:
            return list(csv.reader(f))

    def test_a_tab_whose_names_match_the_csv_is_copied(self):
        printed = self.run_tool(REQUIREMENTS)
        self.assertIn("1 pickups updated from the sheet: 1", printed)
        self.assertEqual(self.csv_rows(), [CSV_HEADER, LUCKY_CHARM])

    def test_a_tab_with_an_added_or_moved_column_changes_nothing(self):
        bat = REQUIREMENTS.index("Bat") + 1
        added = [*REQUIREMENTS[:bat], "BDash", *REQUIREMENTS[bat:]]
        moved = ["None", "Slide", "Glide", *REQUIREMENTS[3:]]
        cases = {"the sheet has 'BDash' where the CSV has 'Impossible'": added,
                 "the sheet has 'Slide' where the CSV has 'Glide'": moved}
        for difference, requirements in cases.items():
            with self.subTest(difference):
                with self.assertRaises(SystemExit) as stop:
                    self.run_tool(requirements)
                self.assertIn(difference, str(stop.exception.code))
                self.assertEqual(self.csv_rows(), [CSV_HEADER, OLD_LUCKY_CHARM])

    def test_the_sixth_combo_column_matches_with_or_without_a_name(self):
        # The CSV's header can leave "Misc. combo 6" out until the sheet uses that column.
        names = ["pickup_number", "Room", "Item Name", "", "dest_room_identifier", *REQUIREMENTS]
        for sixth in ("", "Misc. combo 6"):
            for header in (CSV_HEADER, CSV_HEADER[:-1]):
                self.assertEqual(importer.header_mismatches([*names, sixth], header), [])
        self.assertEqual(importer.header_mismatches([*names, "Notes"], CSV_HEADER),
                         ["the sheet has 'Notes' where the CSV has 'Misc. combo 6'"])


if __name__ == "__main__":
    unittest.main()
