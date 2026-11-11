"""
The automated routing benchmark: how the requirement sheets become questions, and which rooms
are held out.

The tests that read the sheets check rules that hold however the sheets are edited. They pin no
counts, so adding or correcting a row does not fail them. The ROM test reads the USA ROM, and
skips when it is not found.

    python -m pytest worlds/cvaos/test/test_autoroute_benchmark.py -v
"""
from __future__ import annotations

import unittest

from ..data import entrance_info
from ..data.routing_info import AbilityCombo
from ..regions import SPECIAL_ROOM_ENTRANCE_RULES
from ..tools.autoroute import benchmark
from ..tools.autoroute.benchmark import ConditionalWay
from . import find_rom


def mask(*names: str) -> int:
    out = 0
    for name in names:
        out |= int(AbilityCombo[name])
    return out


def ways(*lists: list[str]) -> tuple[frozenset[str], ...]:
    return tuple(frozenset(way) for way in lists)


class TestWays(unittest.TestCase):
    def test_minimal_drops_a_way_that_contains_another(self):
        kept = benchmark.minimal([["DJump", "Glide"], ["DJump"], ["Bat"], ["Bat", "DJump"]])
        self.assertEqual(kept, ways(["DJump"], ["Bat"]))

    def test_minimal_puts_short_ways_first_then_follows_the_token_order(self):
        kept = benchmark.minimal([["Bat"], ["Slide", "DJump"], ["Glide"], ["Glide"]])
        self.assertEqual(kept, ways(["Glide"], ["Bat"], ["Slide", "DJump"]))

    def test_the_empty_way_is_the_only_minimal_way_beside_any_other(self):
        self.assertEqual(benchmark.minimal([["Bat"], []]), ways([]))

    def test_masks_split_into_plain_and_conditional_ways(self):
        plain, conditional = benchmark.ways_from_masks(
            [mask("DJump", "Glide"), mask("Bat", "QSave"), mask("Bat", "PixPer", "Enemy")])
        self.assertEqual(plain, ways(["Glide", "DJump"]))
        self.assertEqual(conditional, (ConditionalWay(frozenset({"Bat"}),
                                                      frozenset({"Enemy", "PixPer"})),
                                       ConditionalWay(frozenset({"Bat"}), frozenset({"QSave"}))))

    def test_an_impossible_mask_is_no_way(self):
        self.assertEqual(benchmark.ways_from_masks([mask("Impossible")]), ((), ()))
        plain, conditional = benchmark.ways_from_masks([mask("Impossible"), mask("DJump")])
        self.assertEqual((plain, conditional), (ways(["DJump"]), ()))

    def test_a_mask_of_zero_is_the_free_way(self):
        self.assertEqual(benchmark.ways_from_masks([0]), (ways([]), ()))

    def test_a_conditional_way_is_dropped_when_a_plain_way_needs_no_more_tokens(self):
        plain, conditional = benchmark.ways_from_masks(
            [mask("DJump"), mask("DJump", "Panth", "QSave"), mask("DJump", "Vert"),
             mask("Glide", "QSave")])
        self.assertEqual(plain, ways(["DJump"]))
        self.assertEqual(conditional, (ConditionalWay(frozenset({"Glide"}), frozenset({"QSave"})),))


class TestQuestions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.questions = benchmark.questions()
        cls.scored = [question for question in cls.questions if not question.skip]

    def test_every_sheet_gives_questions(self):
        self.assertEqual({question.kind for question in self.questions}, set(benchmark.KINDS))

    def test_keys_are_unique(self):
        keys = [question.key for question in self.questions]
        self.assertEqual(len(keys), len(set(keys)))

    def test_ways_hold_tokens_and_conditional_ways_hold_conditions_too(self):
        for question in self.questions:
            for way in question.ways:
                self.assertLessEqual(way, set(benchmark.TOKENS), question.key)
            for way in question.conditional:
                self.assertLessEqual(way.tokens, set(benchmark.TOKENS), question.key)
                self.assertTrue(way.conditions, question.key)
                self.assertLessEqual(way.conditions, set(benchmark.CONDITIONS), question.key)

    def test_the_conditions_are_the_ones_the_module_documents(self):
        # A new AbilityCombo flag lands here until someone decides whether a search holds it,
        # which makes it a token, or cannot, which makes it a condition to describe.
        self.assertEqual(set(benchmark.CONDITIONS), {"Enemy", "PixPer", "Clip", "InZip", "QSave",
                                                     "Floor", "Ceil", "Vert", "Galamoth",
                                                     "Chronomage"})
        self.assertFalse(set(benchmark.CONDITIONS) & set(benchmark.TOKENS))

    def test_a_scored_question_starts_at_a_known_door_of_its_room(self):
        doors = entrance_info.by_door_identifier_unique
        for question in self.scored:
            self.assertIn(question.door, doors, question.key)
            self.assertEqual(question.door.split(":")[0], question.room, question.key)
            if question.kind == "door":
                self.assertIn(question.target, doors, question.key)
                self.assertNotEqual(question.door, question.target, question.key)

    def test_a_row_that_code_replaces_is_skipped(self):
        replaced = {(f"{room}:{start}", f"{room}:{end}")
                    for room, start, end in SPECIAL_ROOM_ENTRANCE_RULES}
        for question in self.questions:
            if question.kind == "door" and (question.door, question.target) in replaced:
                self.assertEqual(question.skip, benchmark.RULE_IN_CODE, question.key)

    def test_a_question_without_a_readable_requirement_is_skipped_and_has_no_way(self):
        for question in self.questions:
            if question.skip == benchmark.NO_LABEL:
                self.assertEqual((question.ways, question.conditional), ((), ()), question.key)


class TestSplit(unittest.TestCase):
    def test_held_out_rooms_are_rooms_of_the_castle(self):
        rooms = {door.room_identifier for door in entrance_info.rows}
        held = benchmark.held_out_rooms()
        self.assertLessEqual(held, rooms)
        self.assertTrue(0.2 < len(held) / len(rooms) < 0.4, len(held))

    def test_both_sides_have_every_sort_of_door_and_pickup_question(self):
        for kind in ("door", "pickup"):
            for side in (False, True):
                mine = [question for question in benchmark.questions() if not question.skip
                        and question.kind == kind and benchmark.held_out(question.room) == side]
                free = [question for question in mine if frozenset() in question.ways]
                impossible = [question for question in mine
                              if not question.ways and not question.conditional]
                self.assertTrue(free, (kind, side))
                self.assertTrue(impossible, (kind, side))
                self.assertGreater(len(mine), len(free) + len(impossible), (kind, side))

    def test_a_group_is_held_out_whole_or_not_at_all(self):
        groups = [[f"{group:02X}{room}" for room in range(1 + group % 4)] for group in range(200)]
        chosen = set(benchmark.choose_held_out(groups))
        for group in groups:
            self.assertIn(len(chosen.intersection(group)), (0, len(group)))
        share = sum(1 for group in groups if chosen.intersection(group)) / len(groups)
        self.assertTrue(0.2 < share < 0.4, share)

    def test_the_choice_does_not_depend_on_the_order_of_groups_or_of_their_rooms(self):
        groups = [[f"{group:02X}{room}" for room in range(3)] for group in range(50)]
        shuffled = [list(reversed(group)) for group in reversed(groups)]
        self.assertEqual(benchmark.choose_held_out(groups), benchmark.choose_held_out(shuffled))

    def test_the_file_text_reads_back_as_the_same_rooms(self):
        text = benchmark.held_out_text(["00F", "003"])
        rooms = [line for line in text.splitlines() if not line.startswith("#")]
        self.assertEqual(rooms, ["003", "00F"])

    def test_the_held_out_file_is_what_the_rom_gives(self):
        path = find_rom()
        if path is None:
            self.skipTest("USA ROM not available")
        with open(path, "rb") as handle:
            rom = handle.read()
        groups = benchmark.layout_groups(rom)
        grouped = sorted(room for group in groups for room in group)
        self.assertEqual(grouped, sorted({door.room_identifier for door in entrance_info.rows}))
        self.assertEqual(
            sorted(benchmark.held_out_rooms()), benchmark.choose_held_out(groups),
            "run `python -m worlds.cvaos.tools.autoroute split --write`, and score every search"
            " again, because the held-out rooms have changed")


if __name__ == "__main__":
    unittest.main()
