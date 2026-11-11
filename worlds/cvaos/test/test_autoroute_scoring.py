"""
The automated routing scorer: how an answer is compared with a sheet's label.

The scoring tests build their own questions, so they do not depend on what the sheets hold. The
baseline tests read the sheets, and check rules that hold however the sheets are edited.

    python -m pytest worlds/cvaos/test/test_autoroute_scoring.py -v
"""
from __future__ import annotations

import json
import os
import random
import tempfile
import unittest

from ..tools.autoroute import __main__ as command_line
from ..tools.autoroute import benchmark, scoring
from ..tools.autoroute.benchmark import TOKENS, ConditionalWay, Question
from ..tools.autoroute.scoring import LOADOUTS

HALF, QUARTER = LOADOUTS // 2, LOADOUTS // 4


def question(plain=(), conditional=(), kind: str = "door", target: str = "001:002",
             skip: str = "") -> Question:
    """Build a question from lists of tokens, and ``(tokens, conditions)`` pairs."""
    return Question(
        kind, "001", "001:000", target, "a test question",
        tuple(frozenset(way) for way in plain),
        tuple(ConditionalWay(frozenset(tokens), frozenset(conditions))
              for tokens, conditions in conditional),
        skip)


def ways(*lists: list[str]) -> tuple[frozenset[str], ...]:
    return tuple(frozenset(way) for way in lists)


class TestLabel(unittest.TestCase):
    def counts(self, asked: Question, granted=()) -> tuple[int, int, int]:
        sheet = scoring.label(asked, granted)
        return sheet.accepted.bit_count(), sheet.conditional.bit_count(), sheet.rejected.bit_count()

    def test_a_free_question_accepts_every_loadout(self):
        self.assertEqual(self.counts(question([[]])), (LOADOUTS, 0, 0))

    def test_an_impossible_question_rejects_every_loadout(self):
        self.assertEqual(self.counts(question()), (0, 0, LOADOUTS))

    def test_one_token_is_accepted_by_half_the_loadouts(self):
        self.assertEqual(self.counts(question([["DJump"]])), (HALF, 0, HALF))

    def test_a_conditional_way_claims_only_what_no_plain_way_accepts(self):
        asked = question([["Bat"]], [(["DJump"], ["QSave"])])
        self.assertEqual(self.counts(asked), (HALF, QUARTER, QUARTER))

    def test_a_granted_condition_makes_its_way_plain(self):
        asked = question([["Bat"]], [(["DJump"], ["QSave"])])
        self.assertEqual(self.counts(asked, ["QSave"]), (HALF + QUARTER, 0, QUARTER))
        self.assertEqual(scoring.label(asked, ["QSave"]).ways, ways(["DJump"], ["Bat"]))

    def test_a_way_stays_conditional_until_all_its_conditions_are_granted(self):
        asked = question(conditional=[(["DJump"], ["Enemy", "PixPer"])])
        self.assertEqual(self.counts(asked, ["PixPer"]), (0, HALF, HALF))
        self.assertEqual(self.counts(asked, ["PixPer", "Enemy"]), (HALF, 0, HALF))

    def test_granting_something_that_is_no_condition_is_an_error(self):
        with self.assertRaises(ValueError):
            scoring.label(question(), ["DJump"])


class TestJudge(unittest.TestCase):
    def test_the_sheets_own_ways_are_exact(self):
        verdict = scoring.judge(question([["DJump"], ["Bat"]]), [["Bat"], ["DJump"]])
        self.assertTrue(verdict.exact)
        self.assertEqual((verdict.lenient, verdict.strict, verdict.conditional), (0, 0, 0))

    def test_an_answer_that_needs_less_is_lenient(self):
        verdict = scoring.judge(question([["DJump", "Glide"]]), [["DJump"]])
        self.assertEqual((verdict.lenient, verdict.strict), (QUARTER, 0))
        self.assertEqual(verdict.lenient_ways, ways(["DJump"]))
        self.assertFalse(verdict.exact)

    def test_an_answer_that_needs_more_is_strict(self):
        verdict = scoring.judge(question([["DJump"]]), [["DJump", "Glide"]])
        self.assertEqual((verdict.lenient, verdict.strict), (0, QUARTER))
        self.assertEqual(verdict.missed_ways, ways(["DJump"]))

    def test_an_answer_can_be_wrong_in_both_directions(self):
        verdict = scoring.judge(question([["DJump"]]), [["Bat"]])
        self.assertEqual((verdict.lenient, verdict.strict), (QUARTER, QUARTER))
        self.assertEqual(verdict.lenient_ways, ways(["Bat"]))
        self.assertEqual(verdict.missed_ways, ways(["DJump"]))

    def test_nothing_proven_is_exact_on_an_impossible_question(self):
        self.assertTrue(scoring.judge(question(), []).exact)

    def test_nothing_proven_misses_every_way_of_the_sheet(self):
        verdict = scoring.judge(question([["DJump"], ["Bat"]]), [])
        self.assertEqual(verdict.strict, HALF + QUARTER)
        self.assertEqual(verdict.missed_ways, ways(["DJump"], ["Bat"]))

    def test_a_question_not_tried_has_no_errors_and_is_not_exact(self):
        verdict = scoring.judge(question([["DJump"]]), None)
        self.assertFalse(verdict.answered)
        self.assertFalse(verdict.exact)
        self.assertEqual((verdict.accepted, verdict.rejected), (HALF, HALF))
        self.assertEqual((verdict.lenient, verdict.strict), (0, 0))

    def test_a_loadout_in_the_conditional_group_counts_in_neither_direction(self):
        asked = question(conditional=[(["DJump"], ["PixPer"])])
        found = scoring.judge(asked, [["DJump"]])
        self.assertEqual((found.lenient, found.strict, found.conditional), (0, 0, HALF))
        self.assertEqual(found.conditional_ways, ways(["DJump"]))
        self.assertTrue(found.exact)
        self.assertTrue(scoring.judge(asked, []).exact)

    def test_granting_the_condition_turns_the_same_answers_into_right_and_strict(self):
        asked = question(conditional=[(["DJump"], ["PixPer"])])
        self.assertEqual(scoring.judge(asked, [["DJump"]], ["PixPer"]).conditional, 0)
        self.assertEqual(scoring.judge(asked, [], ["PixPer"]).strict, HALF)

    def test_a_question_with_every_loadout_conditional_is_unscorable(self):
        asked = question(conditional=[([], ["Vert"])])
        verdict = scoring.judge(asked, [[]])
        self.assertFalse(verdict.scorable)
        self.assertEqual(verdict.conditional, LOADOUTS)
        self.assertTrue(scoring.judge(asked, [[]], ["Vert"]).scorable)

    def test_a_redundant_way_in_an_answer_changes_nothing(self):
        asked = question([["DJump", "Glide"]])
        self.assertEqual(scoring.judge(asked, [["DJump"], ["DJump", "Bat"], ["DJump"]]),
                         scoring.judge(asked, [["DJump"]]))

    def test_text_where_a_list_belongs_is_an_error(self):
        for answer in ("Bat", ["Bat"], [["Bat"], "DJump"], {"Bat": 1}):
            with self.assertRaises(ValueError, msg=repr(answer)):
                scoring.judge(question(), answer)

    def test_a_name_that_is_no_token_is_an_error(self):
        for answer in ([["Fly"]], [["QSave"]], [["bat"]]):
            with self.assertRaises(ValueError, msg=repr(answer)):
                scoring.judge(question(), answer)

    def test_accepts_follows_the_ways(self):
        self.assertTrue(scoring.accepts([["DJump", "Glide"], ["Bat"]], ["Bat", "Slide"]))
        self.assertFalse(scoring.accepts([["DJump", "Glide"], ["Bat"]], ["DJump", "Slide"]))
        self.assertTrue(scoring.accepts([[]], []))
        self.assertFalse(scoring.accepts([], TOKENS))


class TestAgainstCounting(unittest.TestCase):
    """The scorer's counts equal what going through the loadouts one at a time gives."""

    def random_way(self, rng: random.Random) -> list[str]:
        return rng.sample(TOKENS, rng.choice((0, 1, 1, 2, 2, 3)))

    def count(self, asked: Question, answer, granted: set[str]) -> tuple[int, int, int]:
        met = [way.tokens for way in asked.conditional if way.conditions <= granted]
        unmet = [way.tokens for way in asked.conditional if not way.conditions <= granted]
        plain = [*asked.ways, *met]
        lenient = strict = conditional = 0
        for number in range(LOADOUTS):
            loadout = {token for position, token in enumerate(TOKENS) if number >> position & 1}
            sheet_accepts = any(way <= loadout for way in plain)
            sheet_unsure = not sheet_accepts and any(way <= loadout for way in unmet)
            answer_accepts = any(set(way) <= loadout for way in answer)
            lenient += answer_accepts and not sheet_accepts and not sheet_unsure
            strict += sheet_accepts and not answer_accepts
            conditional += answer_accepts and sheet_unsure
        return lenient, strict, conditional

    def test_random_questions_and_answers(self):
        rng = random.Random(20261111)
        conditions = ("QSave", "PixPer", "Vert")
        for case in range(60):
            asked = question(
                [self.random_way(rng) for _ in range(rng.randrange(3))],
                [(self.random_way(rng), rng.sample(conditions, rng.choice((1, 1, 2))))
                 for _ in range(rng.randrange(3))])
            answer = [self.random_way(rng) for _ in range(rng.randrange(4))]
            granted = set(rng.sample(conditions, rng.randrange(3)))
            verdict = scoring.judge(asked, answer, granted)
            self.assertEqual((verdict.lenient, verdict.strict, verdict.conditional),
                             self.count(asked, answer, granted), (case, asked, answer, granted))
            sheet = scoring.label(asked, granted)
            groups = (sheet.accepted, sheet.conditional, sheet.rejected)
            self.assertEqual(sum(group.bit_count() for group in groups), LOADOUTS)
            self.assertEqual(groups[0] | groups[1] | groups[2], (1 << LOADOUTS) - 1)
            self.assertEqual(verdict.exact, not verdict.lenient and not verdict.strict)
            self.assertEqual(bool(verdict.lenient), bool(verdict.lenient_ways))
            self.assertEqual(bool(verdict.strict), bool(verdict.missed_ways))
            if verdict.conditional_ways:
                self.assertTrue(verdict.conditional)
            sorted_ways = {*verdict.lenient_ways, *verdict.conditional_ways}
            self.assertLessEqual(sorted_ways, set(scoring.answer_ways(answer)))
            self.assertFalse(set(verdict.lenient_ways) & set(verdict.conditional_ways))


class TestScore(unittest.TestCase):
    def setUp(self):
        self.first = question([["DJump"]])
        self.second = question([[]], target="001:003")
        self.skipped = question([["Bat"]], target="001:004", skip=benchmark.UNKNOWN_DOOR)

    def test_verdicts_follow_the_questions_and_a_missing_answer_is_not_tried(self):
        verdicts = scoring.score([self.first, self.second], {self.second.key: [[]]})
        self.assertEqual([verdict.question for verdict in verdicts], [self.first, self.second])
        self.assertEqual([verdict.answered for verdict in verdicts], [False, True])

    def test_null_is_not_tried_and_an_empty_list_is_nothing_proven(self):
        verdicts = scoring.score([self.first, self.second],
                                 {self.first.key: None, self.second.key: []})
        self.assertFalse(verdicts[0].answered)
        self.assertEqual(verdicts[1].strict, LOADOUTS)

    def test_a_skipped_question_is_never_judged(self):
        verdict, = scoring.score([self.skipped], {self.skipped.key: [[]]})
        self.assertFalse(verdict.answered)
        self.assertFalse(verdict.scorable)

    def test_a_key_that_names_no_question_is_an_error(self):
        with self.assertRaises(ValueError):
            scoring.score([self.first], {"door|001:000|001:0O2": [[]]})

    def test_an_answer_to_a_benchmark_question_outside_the_list_is_ignored(self):
        other = benchmark.questions()[0]
        verdict, = scoring.score([self.first], {other.key: [[]], self.first.key: [["DJump"]]})
        self.assertTrue(verdict.exact)

    def test_a_bad_answer_names_its_question(self):
        with self.assertRaisesRegex(ValueError, "001:002.*Fly"):
            scoring.score([self.first], {self.first.key: [["Fly"]]})


class TestReport(unittest.TestCase):
    def setUp(self):
        self.questions = [
            question([["DJump"]], target="1"),
            question([["DJump"]], target="2"),
            question([["DJump"]], target="3"),
            question([], target="4", kind="pickup"),
            question(conditional=[([], ["Vert"])], target="5", kind="enemy"),
            question([[]], target="6", skip=benchmark.NO_LABEL),
        ]
        answers = {self.questions[0].key: [["DJump"]], self.questions[1].key: [[]],
                   self.questions[3].key: [], self.questions[4].key: [[]]}
        self.verdicts = scoring.score(self.questions, answers)

    def test_the_tally_counts_questions(self):
        doors = scoring.tally(self.verdicts)["door"]
        self.assertEqual((doors.questions, doors.answered, doors.exact), (3, 2, 1))
        self.assertEqual((doors.lenient, doors.strict, doors.skipped), (1, 0, 1))
        self.assertEqual(doors.lenient_share, 0.5)
        self.assertEqual(doors.strict_share, 0.0)

    def test_a_share_with_nothing_under_it_is_none(self):
        pickups = scoring.tally(self.verdicts)["pickup"]
        self.assertEqual((pickups.questions, pickups.exact), (1, 1))
        self.assertEqual(pickups.lenient_share, 0.0)
        self.assertIsNone(pickups.strict_share)

    def test_an_unscorable_question_is_counted_apart(self):
        enemies = scoring.tally(self.verdicts)["enemy"]
        self.assertEqual((enemies.questions, enemies.unscorable, enemies.conditional), (0, 1, 1))

    def test_the_total_adds_the_kinds_up(self):
        counts = scoring.tally(self.verdicts)
        self.assertEqual(counts["all"].questions,
                         sum(counts[kind].questions for kind in benchmark.KINDS))
        self.assertEqual((counts["all"].skipped, counts["all"].unscorable), (1, 1))

    def test_the_report_has_a_line_for_each_kind_and_names_what_it_left_out(self):
        lines = scoring.report(self.verdicts).splitlines()
        self.assertEqual([line.split()[0] for line in lines[1:5]], [*benchmark.KINDS, "all"])
        self.assertIn("50.00%", lines[1])
        self.assertIn("1 skipped rows and 1 questions", lines[-1])

    def test_disagreements_name_the_question_and_both_sets_of_ways(self):
        lenient = scoring.disagreements(self.verdicts, "lenient")
        self.assertEqual(len(lenient), 1)
        self.assertIn(self.questions[1].key, lenient[0])
        self.assertIn("lenient: free", lenient[0])
        self.assertIn("sheet: DJump", lenient[0])
        self.assertEqual(scoring.disagreements(self.verdicts, "strict"), [])
        conditional, = scoring.disagreements(self.verdicts, "conditional")
        self.assertIn("free with Vert", conditional)


class TestBaselines(unittest.TestCase):
    """What must hold on the real sheets for the two baselines at the ends."""

    @classmethod
    def setUpClass(cls):
        cls.questions = benchmark.questions()

    def test_answering_free_is_never_strict(self):
        verdicts = scoring.score(self.questions, scoring.free_answers(self.questions))
        self.assertEqual(scoring.tally(verdicts)["all"].strict, 0)

    def test_answering_nothing_is_never_lenient(self):
        verdicts = scoring.score(self.questions, scoring.strict_answers(self.questions))
        counts = scoring.tally(verdicts)["all"]
        self.assertEqual((counts.lenient, counts.conditional), (0, 0))

    def test_the_sheets_own_ways_are_exact_everywhere(self):
        answers = {asked.key: [sorted(way) for way in asked.ways] for asked in self.questions}
        counts = scoring.tally(scoring.score(self.questions, answers))["all"]
        self.assertEqual(counts.exact, counts.questions)
        self.assertGreater(counts.questions, 0)

    def test_every_baseline_answers_every_scored_question(self):
        for name, baseline in scoring.BASELINES.items():
            counts = scoring.tally(scoring.score(self.questions, baseline(self.questions)))["all"]
            self.assertEqual(counts.answered, counts.questions, name)


class TestCommandLine(unittest.TestCase):
    def test_dev_and_held_out_rooms_divide_the_questions(self):
        dev, held = command_line.chosen("dev"), command_line.chosen("held-out")
        self.assertTrue(dev and held)
        self.assertEqual(len(dev) + len(held), len(command_line.chosen("all")))
        self.assertFalse({asked.room for asked in dev} & {asked.room for asked in held})

    def test_a_record_is_json_and_can_leave_the_labels_out(self):
        asked = question([["DJump", "Glide"]], [(["Bat"], ["QSave"])])
        record = json.loads(json.dumps(command_line.record(asked)))
        self.assertEqual(record["key"], asked.key)
        self.assertEqual(record["ways"], [["Glide", "DJump"]])
        self.assertEqual(record["conditional"], [{"tokens": ["Bat"], "conditions": ["QSave"]}])
        bare = command_line.record(asked, labels=False)
        self.assertNotIn("ways", bare)
        self.assertNotIn("conditional", bare)

    def test_recorded_ways_score_as_exact_when_read_back(self):
        asked = question([["DJump", "Glide"], ["Bat"]])
        self.assertTrue(scoring.judge(asked, command_line.record(asked)["ways"]).exact)

    def test_answers_are_read_from_a_json_object(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "answers.json")
            written = {"door|001:000|001:002": [["DJump"]], "door|001:000|001:003": None}
            with open(path, "w", encoding="utf-8-sig") as handle:
                json.dump(written, handle)
            self.assertEqual(scoring.load_answers(path), written)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump([["DJump"]], handle)
            with self.assertRaises(ValueError):
                scoring.load_answers(path)


if __name__ == "__main__":
    unittest.main()
