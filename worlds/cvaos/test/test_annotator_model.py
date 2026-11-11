"""
The annotator turns its actions into cell edits that keep each row self-contained, and its
worklist leaves out pairs that other doors already make free.

    python -m pytest worlds/cvaos/test/test_annotator_model.py -v
"""
from __future__ import annotations

import datetime
import unittest

from ..tools.annotator import model, worklist
from ..tools.annotator.model import EditError, EnemyTab, Requirement

BLOCK = ["enemy_number", "room_id", "Enemy Name", "Specifier", "x", "-y", "Prog.?",
         "dest_room_identifier", "None", "Glide", "Slide", "DJump", "HJump", "WWalk", "Dive",
         "Panth.", "Bat", "Impossible", "Kick", "<alt 2>", "Misc. combo 1", "Misc. combo 2",
         "Misc. combo 3", "Misc. combo 4", "Misc. combo 5"]
EXTRA = ["Status", "Shared enemies", "Shared entrances", "Notes", "Annotated by", "Date",
         "Verified by"]
TODAY = datetime.date(2026, 11, 10)


def grid(*rows: dict[str, str]) -> list[list[str]]:
    """Return a tab laid out like the live one: two tracker columns, the block, then EXTRA.

    The block starts at column C, so "dest_room_identifier" is J, "DJump" is N, the first combo
    is W, and EXTRA runs from AC (Status) to AI (Verified by), after a "Req." column at AB.
    """
    header = ["", "41", *BLOCK, "Req.", *EXTRA]
    out = [header]
    for values in rows:
        cells = {name: "FALSE" for name in BLOCK[8:20]}
        cells.update(values)
        out.append(["", "", *(cells.get(name, "") for name in BLOCK), "",
                    *(cells.get(name, "") for name in EXTRA)])
    return out


def enemy(number: int, name: str, **cells: str) -> dict[str, str]:
    return {"enemy_number": str(number), "room_id": "90D", "Enemy Name": name, "x": "240",
            "-y": "286", **cells}


class TabTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tab = EnemyTab(grid(
            enemy(751, "Succubus", dest_room_identifier="917", DJump="TRUE", Status="Tested"),
            enemy(752, "Gargoyle"),
            enemy(753, "Succubus", dest_room_identifier="917", Bat="TRUE", Status="Judged"),
        ))

    def test_rows_are_read_by_name_and_sheet_row(self):
        self.assertEqual([(row.number, row.enemy) for row in self.tab.rows],
                         [(2, 751), (3, 752), (4, 753)])
        requirement = self.tab.requirement(self.tab.row(751, "917"))
        self.assertEqual((requirement.ticks, requirement.status), (frozenset({"DJump"}), "Tested"))

    def test_record_writes_booleans_combos_and_a_date(self):
        edit = self.tab.record(752, "", Requirement(frozenset({"HJump"}), ("Malphas (DJump)",),
                                                    "Judged"), annotator="dem", today=TODAY,
                               new_doors_cell="90C")
        writes = dict(edit.writes)
        self.assertEqual(writes["'EnemyRequirements'!J3"], [["90C"]])      # doors
        self.assertEqual(writes["'EnemyRequirements'!O3"], [[True]])       # HJump
        self.assertEqual(writes["'EnemyRequirements'!W3"], [["Malphas (DJump)"]])
        # Cells that already hold the value aren't touched: DJump was unticked, combo 2 empty.
        self.assertNotIn("'EnemyRequirements'!N3", writes)
        self.assertEqual(edit.clears, [])
        self.assertEqual(edit.dated, [("'EnemyRequirements'!AH3", [["2026-11-10"]])])

    def test_record_refreshes_the_shared_columns(self):
        edit = self.tab.record(753, "917", Requirement(frozenset({"DJump"}), (), "Tested"))
        writes = dict(edit.writes)
        self.assertEqual(writes["'EnemyRequirements'!AD2"], [["753 Succubus"]])   # Shared enemies
        self.assertEqual(writes["'EnemyRequirements'!AD4"], [["751 Succubus"]])

    def test_same_for_enemy_fills_a_blank_row(self):
        edit = self.tab.same_for_enemies(751, "917", [752], annotator="dem")
        writes = dict(edit.writes)
        self.assertEqual(writes["'EnemyRequirements'!J3"], [["917"]])
        self.assertEqual(writes["'EnemyRequirements'!N3"], [[True]])       # DJump
        self.assertEqual(writes["'EnemyRequirements'!AC3"], [["Tested"]])  # Status

    def test_same_for_enemy_asks_before_overwriting(self):
        with self.assertRaisesRegex(EditError, "confirm to overwrite"):
            self.tab.same_for_enemies(751, "917", [753])
        self.assertTrue(self.tab.same_for_enemies(751, "917", [753], overwrite=True).writes)

    def test_same_for_enemy_without_a_free_row_asks_for_one(self):
        tab = EnemyTab(grid(
            enemy(751, "Succubus", dest_room_identifier="917", DJump="TRUE", Status="Tested"),
            enemy(752, "Gargoyle", dest_room_identifier="90C", Bat="TRUE", Status="Tested")))
        edit = tab.same_for_enemies(751, "917", [752])
        self.assertEqual((edit.insert_after, edit.writes), ([3], []))
        filled = dict(tab.fill_new_row(752, 4).writes)
        self.assertEqual(filled["'EnemyRequirements'!C4"], [[752]])
        self.assertEqual(filled["'EnemyRequirements'!D4"], [["90D"]])

    def test_same_for_enemy_checks_doors_before_asking_for_a_row(self):
        tab = EnemyTab(grid(
            enemy(751, "Succubus", dest_room_identifier="917", DJump="TRUE", Status="Tested"),
            enemy(752, "Gargoyle", dest_room_identifier="Any", Bat="TRUE", Status="Tested")))
        with self.assertRaisesRegex(EditError, "already has a row naming 90D:917"):
            tab.same_for_enemies(751, "917", [752])

    def test_same_for_entrance_adds_to_the_doors_cell(self):
        edit = self.tab.same_for_entrances(751, "917", ["90C"])
        self.assertEqual(dict(edit.writes)["'EnemyRequirements'!J2"], [["917,90C"]])

    def test_a_door_cannot_be_in_two_rows_of_one_placement(self):
        tab = EnemyTab(grid(
            enemy(751, "Succubus", dest_room_identifier="917", DJump="TRUE", Status="Tested"),
            enemy(751, "Succubus", dest_room_identifier="90C", Bat="TRUE", Status="Tested")))
        with self.assertRaisesRegex(EditError, "already names 90D:90C"):
            tab.same_for_entrances(751, "917", ["90C"])

    def test_an_unknown_status_is_refused(self):
        with self.assertRaisesRegex(EditError, "Status must be"):
            self.tab.record(752, "", Requirement(frozenset({"Bat"}), (), "Done"))


class WorklistTest(unittest.TestCase):
    def test_a_pair_implied_free_through_another_door_is_finished(self):
        tab = EnemyTab(grid(enemy(751, "Succubus", dest_room_identifier="917", None_="",
                                  **{"None": "TRUE"}, Status="Tested")))
        recorded = tab.pair_requirements()
        # From 90C the room's routing reaches 917 with nothing, and from 90E only with Bat.
        paths = {"90D:90C": {"90D:917": {0}}, "90D:90E": {"90D:917": {128}}, "90D:917": {}}
        pairs = {pair.door: pair for pair in worklist.room_pairs(tab, "90D", recorded, paths)}
        self.assertEqual(pairs["90D:917"].state, "annotated")
        self.assertEqual((pairs["90D:90C"].state, pairs["90D:90C"].masks), ("implied", (0,)))
        self.assertEqual((pairs["90D:90E"].state, pairs["90D:90E"].masks), ("implied", (128,)))

    def test_add_keeps_only_minimal_masks(self):
        kept: set[int] = set()
        for mask in (0b110, 0b010, 0b011, 0b100):
            worklist._add(kept, mask)
        self.assertEqual(kept, {0b010, 0b100})


if __name__ == "__main__":
    unittest.main()
