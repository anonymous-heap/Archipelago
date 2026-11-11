"""
Traces of the automated routing search: recording the operations run on one emulator, saving and
loading them, replaying them on another emulator, and answering from them alone.

The emulators here are fakes whose frame callback counts frames in memory, so two of them agree
exactly when their callbacks do. Trace files are written to a temporary folder. No ROM is read.

    python -m pytest worlds/cvaos/test/test_autoroute_traces.py -v
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest

from ..tools.annotator import emulator as annotator
from ..tools.autoroute import game
from ..tools.autoroute.emulator import FakeEmulator
from ..tools.autoroute.game import Session
from ..tools.autoroute.traces import (EWRAM, FORMAT, Mismatch, Playback, Recording, TraceError,
                                      load_trace, replay, save_trace)
from .test_autoroute_game import fake_game

COUNTER = 0x02000100   # the frames stepped, which the counting fakes keep here
HELD_A = 0x02000101    # 1 while A was held on the last frame
SCRATCH = 0x02000200


def counting(by: int = 1) -> FakeEmulator:
    """Return a fake that adds ``by`` to COUNTER on each frame and notes whether A is held."""
    def on_frame(fake: FakeEmulator, held: tuple[str, ...]) -> None:
        fake.write(COUNTER, bytes([(fake.read(COUNTER, 1)[0] + by) % 256]))
        fake.write(HELD_A, b"\x01" if "A" in held else b"\x00")

    return FakeEmulator(on_frame)


def record_run(emulator: FakeEmulator) -> list[list]:
    """Run a fixed set of operations through a Recording, and return its trace."""
    recording = Recording(emulator)
    recording.reset()
    recording.note("start")
    recording.write(SCRATCH, b"\x2a")
    recording.step(["A"], 3)
    recording.read(COUNTER, 2)
    state = recording.save()
    recording.step([], 2)
    recording.read(COUNTER, 2)
    recording.load(state)
    recording.read(COUNTER, 1)
    recording.digest(0x02000000, 0x400)
    recording.drop(state)
    return recording.ops


def address(value: int) -> str:
    return f"{value:#010x}"


class TestRecording(unittest.TestCase):
    def test_reads_and_writes_reach_the_emulator_and_are_recorded_with_their_bytes(self):
        fake = FakeEmulator()
        recording = Recording(fake)
        recording.write(0x02000088, b"\x01\x02")
        self.assertEqual(recording.read(0x02000088, 3), b"\x01\x02\x00")
        self.assertEqual(fake.read(0x02000088, 2), b"\x01\x02")
        self.assertEqual(recording.ops, [["write", "0x02000088", "0102"],
                                         ["read", "0x02000088", "010200"]])

    def test_a_batched_read_or_write_is_recorded_as_one_operation_per_range(self):
        recording = Recording(FakeEmulator())
        recording.write_many([(SCRATCH, b"\x05"), (SCRATCH + 4, b"\x06\x07")])
        blocks = recording.read_many([(SCRATCH + 4, 2), (SCRATCH, 1)])
        self.assertEqual(blocks, [b"\x06\x07", b"\x05"])
        self.assertEqual(recording.ops, [["write", address(SCRATCH), "05"],
                                         ["write", address(SCRATCH + 4), "0607"],
                                         ["read", address(SCRATCH + 4), "0607"],
                                         ["read", address(SCRATCH), "05"]])

    def test_a_step_records_its_buttons_in_the_order_of_buttons_and_its_frames(self):
        fake = counting()
        recording = Recording(fake)
        recording.step(["Left", "A"], 3)
        recording.step()
        self.assertEqual(recording.ops, [["step", ["A", "Left"], 3], ["step", [], 1]])
        self.assertEqual(fake.frame, 4)

    def test_a_step_with_an_unknown_button_raises_and_records_nothing(self):
        recording = Recording(counting())
        with self.assertRaises(ValueError):
            recording.step(["Turbo"])
        self.assertEqual(recording.ops, [])

    def test_state_numbers_count_from_zero_and_do_not_start_again_at_a_reset(self):
        recording = Recording(FakeEmulator())
        self.assertEqual([recording.save(), recording.save()], [0, 1])
        recording.reset()
        self.assertEqual(recording.save(), 2)
        self.assertEqual(recording.ops, [["save", 0], ["save", 1], ["reset"], ["save", 2]])

    def test_load_and_drop_take_the_recordings_own_numbers(self):
        fake = FakeEmulator()
        recording = Recording(fake)
        fake.save()   # so that the fake's own handles differ from the recording's numbers
        state = recording.save()
        recording.write(SCRATCH, b"\x09")
        recording.load(state)
        self.assertEqual(fake.read(SCRATCH, 1), b"\x00")
        recording.drop(state)
        self.assertEqual(recording.ops[-2:], [["load", state], ["drop", state]])
        with self.assertRaises(KeyError):
            recording.load(state)

    def test_a_state_saved_before_a_reset_cannot_be_loaded_after_it(self):
        recording = Recording(FakeEmulator())
        state = recording.save()
        recording.reset()
        with self.assertRaises(KeyError):
            recording.load(state)

    def test_a_digest_records_the_md5_of_the_memory_and_no_read(self):
        fake = FakeEmulator()
        fake.write(0x02000010, b"\x04")
        recording = Recording(fake)
        found = recording.digest(0x02000000, 0x40)
        self.assertEqual(found, hashlib.md5(fake.read(0x02000000, 0x40)).hexdigest())
        self.assertEqual(recording.ops, [["digest", "0x02000000", 0x40, found]])

    def test_a_note_is_recorded_as_it_is(self):
        recording = Recording(FakeEmulator())
        recording.note("entering 009:008")
        self.assertEqual(recording.ops, [["note", "entering 009:008"]])


class TestTraceFiles(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name

    def path(self, name: str = "run.trace") -> str:
        return os.path.join(self.folder, name)

    def write_text(self, text: str, name: str = "other.trace") -> str:
        with open(self.path(name), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return self.path(name)

    def test_a_saved_trace_loads_back_with_its_description_and_operations(self):
        ops = record_run(counting())
        save_trace(self.path(), {"emulator": "fake", "seed": 3}, ops)
        about, loaded = load_trace(self.path())
        self.assertEqual(about, {"format": FORMAT, "emulator": "fake", "seed": 3})
        self.assertEqual(loaded, ops)

    def test_the_file_is_json_lines_with_the_description_first(self):
        save_trace(self.path(), {"emulator": "fake"}, [["reset"], ["step", ["A"], 3]])
        with open(self.path(), "rb") as handle:
            raw = handle.read()
        self.assertNotIn(b"\r", raw)
        lines = raw.decode("utf-8").splitlines()
        self.assertEqual(json.loads(lines[0]), {"format": FORMAT, "emulator": "fake"})
        self.assertEqual([json.loads(line) for line in lines[1:]], [["reset"], ["step", ["A"], 3]])

    def test_blank_lines_are_passed_over(self):
        path = self.write_text(json.dumps({"format": FORMAT}) + "\n\n" + '["reset"]\n\n')
        self.assertEqual(load_trace(path), ({"format": FORMAT}, [["reset"]]))

    def test_a_json_file_of_another_format_is_not_a_trace(self):
        path = self.write_text(json.dumps({"format": "something-else"}) + "\n" + '["reset"]\n')
        with self.assertRaises(TraceError):
            load_trace(path)

    def test_a_json_object_with_no_format_is_not_a_trace(self):
        with self.assertRaises(TraceError):
            load_trace(self.write_text('{"door": "009:008"}\n'))

    def test_a_file_that_starts_with_an_operation_is_not_a_trace(self):
        with self.assertRaises(TraceError):
            load_trace(self.write_text('["reset"]\n["step", [], 1]\n'))

    def test_an_empty_file_is_not_a_trace(self):
        with self.assertRaises(TraceError):
            load_trace(self.write_text(""))

    def test_a_text_file_that_is_not_json_is_not_a_trace(self):
        with self.assertRaises(TraceError):
            load_trace(self.write_text("door,outcome\n009:008,settles\n"))

    def test_a_later_line_that_is_not_an_operation_is_not_a_trace(self):
        first = json.dumps({"format": FORMAT}) + "\n"
        for line in ('["reset"', "[]", '{"step": 3}', '"reset"'):
            with self.subTest(line=line), self.assertRaisesRegex(TraceError, "other.trace"):
                load_trace(self.write_text(first + '["reset"]\n' + line + "\n"))

    def test_the_description_cannot_name_another_format(self):
        save_trace(self.path(), {"format": "something-else", "emulator": "fake"}, [["reset"]])
        self.assertEqual(load_trace(self.path()),
                         ({"format": FORMAT, "emulator": "fake"}, [["reset"]]))


class TestReplay(unittest.TestCase):
    def test_a_faithful_emulator_gives_no_mismatch(self):
        ops = record_run(counting())
        self.assertEqual(replay(ops, counting()), [])

    def test_a_trace_loaded_from_a_file_replays_the_same(self):
        ops = record_run(counting())
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "run.trace")
            save_trace(path, {}, ops)
            _, loaded = load_trace(path)
        self.assertEqual(replay(loaded, counting()), [])

    def test_each_read_that_differs_is_reported_with_its_index_and_its_frames(self):
        ops = record_run(counting())
        found = replay(ops, counting(by=2), digests=False)
        reads = [index for index, op in enumerate(ops) if op[0] == "read"]
        self.assertEqual([mismatch.index for mismatch in found], reads)
        first = found[0]
        self.assertEqual(first.op, ["read", address(COUNTER), "0301"])
        self.assertEqual((first.frames, first.got), (3, "0601"))
        self.assertEqual(found[1].frames, 5)

    def test_a_mismatch_says_where_it_happened_and_what_differed(self):
        mismatch = Mismatch(4, 3, ["read", "0x02000100", "0301"], "0601")
        self.assertEqual(str(mismatch), "operation 4, 3 frames after the reset: read at 0x02000100"
                                        " gave 0601, and the trace has 0301")

    def test_a_digest_that_differs_is_a_mismatch_unless_digests_are_skipped(self):
        ops = [["write", address(SCRATCH), "01"], ["digest", address(SCRATCH), 4, "0" * 32]]
        found = replay(ops, FakeEmulator())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].got, hashlib.md5(b"\x01\x00\x00\x00").hexdigest())
        self.assertEqual(replay(ops, FakeEmulator(), digests=False), [])

    def test_stop_after_ends_the_replay_at_that_many_mismatches(self):
        ops = [["read", address(SCRATCH), "01"], ["write", address(SCRATCH + 1), "07"],
               ["read", address(SCRATCH), "02"]]
        fake = FakeEmulator()
        self.assertEqual([mismatch.index for mismatch in replay(ops, fake, stop_after=1)], [0])
        self.assertEqual(fake.read(SCRATCH + 1, 1), b"\x00")
        fake = FakeEmulator()
        self.assertEqual([mismatch.index for mismatch in replay(ops, fake)], [0, 2])
        self.assertEqual(fake.read(SCRATCH + 1, 1), b"\x07")

    def test_a_reset_starts_the_frame_count_again(self):
        ops = [["step", [], 4], ["reset"], ["step", [], 2], ["read", address(COUNTER), "ff"]]
        (mismatch,) = replay(ops, counting())
        self.assertEqual((mismatch.frames, mismatch.got), (2, "02"))

    def test_notes_are_ignored_and_an_unknown_operation_raises_trace_error(self):
        self.assertEqual(replay([["note", "anything"]], FakeEmulator()), [])
        with self.assertRaisesRegex(TraceError, "operation 1"):
            replay([["note", "x"], ["poke", "0x02000000", "00"]], FakeEmulator())

    def test_a_load_returns_the_frame_count_to_that_of_the_saved_state(self):
        ops = [["reset"], ["step", [], 3], ["save", 0], ["step", [], 2], ["load", 0],
               ["read", address(COUNTER), "ff"], ["step", [], 4], ["load", 0], ["drop", 0],
               ["step", [], 1], ["read", address(COUNTER), "ff"]]
        first, second = replay(ops, counting())
        self.assertEqual((first.got, first.frames), ("03", 3))
        self.assertEqual((second.got, second.frames), ("04", 4))


class TestPlayback(unittest.TestCase):
    def test_reads_are_answered_from_the_trace(self):
        playback = Playback([["read", "0x02000088", "08f45008"]])
        self.assertEqual(playback.read(0x02000088, 4), bytes.fromhex("08f45008"))
        self.assertTrue(playback.done)

    def test_code_that_was_recorded_runs_again_with_no_emulator(self):
        recording = Recording(fake_game(boot_frames=4, warp_frames=3))
        session = Session(recording)
        frames = session.boot()
        session.set_loadout(["DJump", "Dive"])
        warp = session.teleport(0x08510000, 0, 0, 16, 112)
        arrived = session.state()
        playback = Playback(recording.ops)
        again = Session(playback)
        self.assertEqual(again.boot(), frames)
        again.set_loadout(["DJump", "Dive"])
        self.assertEqual(again.teleport(0x08510000, 0, 0, 16, 112), warp)
        self.assertEqual(again.state(), arrived)
        self.assertTrue(playback.done)

    def test_every_operation_kind_matches_its_own_call(self):
        playback = Playback(record_run(counting()))
        playback.reset()
        playback.write(SCRATCH, b"\x2a")
        playback.step(["A"], 3)
        self.assertEqual(playback.read(COUNTER, 2), b"\x03\x01")
        state = playback.save()
        self.assertEqual(state, 0)
        playback.step([], 2)
        playback.read(COUNTER, 2)
        playback.load(state)
        playback.read(COUNTER, 1)
        playback.digest(0x02000000, 0x400)
        playback.drop(state)
        self.assertTrue(playback.done)

    def test_a_call_out_of_order_raises_trace_error(self):
        playback = Playback([["read", "0x02000088", "00"], ["write", "0x02000088", "01"]])
        with self.assertRaisesRegex(TraceError, "Operation 0"):
            playback.write(0x02000088, b"\x01")

    def test_a_call_with_other_arguments_raises_trace_error(self):
        cases = [(["read", "0x02000088", "00"], lambda p: p.read(0x02000089, 1)),
                 (["write", "0x02000088", "01"], lambda p: p.write(0x02000088, b"\x02")),
                 (["step", ["A"], 3], lambda p: p.step(["A"], 2)),
                 (["step", ["A"], 3], lambda p: p.step(["B"], 3)),
                 (["load", 0], lambda p: p.load(1)),
                 (["drop", 0], lambda p: p.drop(1)),
                 (["digest", "0x02000000", 16, "0" * 32], lambda p: p.digest(0x02000000, 8))]
        for op, call in cases:
            with self.subTest(op=op), self.assertRaises(TraceError):
                call(Playback([op]))

    def test_buttons_match_in_any_order(self):
        Playback([["step", ["A", "Left"], 3]]).step(["Left", "A"], 3)

    def test_a_read_of_another_length_raises_trace_error(self):
        with self.assertRaisesRegex(TraceError, "asked for 4"):
            Playback([["read", "0x02000088", "0001"]]).read(0x02000088, 4)

    def test_a_call_past_the_end_raises_trace_error(self):
        playback = Playback([["reset"]])
        playback.reset()
        with self.assertRaisesRegex(TraceError, "ended"):
            playback.step()

    def test_notes_and_digests_are_passed_over_except_by_digest(self):
        ops = [["note", "a"], ["digest", "0x02000000", 4, "ab" * 16], ["reset"],
               ["note", "b"], ["digest", "0x02000000", 4, "cd" * 16]]
        playback = Playback(ops)
        playback.reset()
        self.assertEqual(playback.digest(0x02000000, 4), "cd" * 16)
        self.assertTrue(playback.done)

    def test_done_is_false_until_every_operation_but_notes_and_digests_is_used(self):
        playback = Playback([["reset"], ["note", "end"], ["digest", "0x02000000", 4, "00" * 16]])
        self.assertFalse(playback.done)
        playback.reset()
        self.assertTrue(playback.done)

    def test_save_returns_the_traces_state_number(self):
        playback = Playback([["save", 5], ["load", 5]])
        state = playback.save()
        self.assertEqual(state, 5)
        playback.load(state)

    def test_asking_whether_playback_is_done_does_not_use_up_a_digest(self):
        playback = Playback([["reset"], ["digest", "0x02000000", 4, "ab" * 16], ["reset"],
                             ["digest", "0x02000000", 4, "cd" * 16]])
        playback.reset()
        self.assertFalse(playback.done)
        self.assertEqual(playback.digest(0x02000000, 4), "ab" * 16)
        playback.reset()
        self.assertTrue(playback.done)
        self.assertEqual(playback.digest(0x02000000, 4), "cd" * 16)

    def test_a_step_line_of_the_trace_may_list_its_buttons_in_any_order(self):
        Playback([["step", ["Left", "A"], 3]]).step(["A", "Left"], 3)
        Playback([["step", ["Left", "A"], 3]]).step(["Left", "A"], 3)

    def test_a_step_line_with_an_unknown_button_is_refused(self):
        with self.assertRaisesRegex(TraceError, "Operation 1"):
            Playback([["reset"], ["step", ["Turbo"], 3]])


class TestEwram(unittest.TestCase):
    def test_every_range_that_a_session_reads_or_writes_is_inside_ewram(self):
        ranges = [*annotator.STATE_READS, *annotator.LOADOUT_READS, annotator.WEAPON_READ,
                  game.PICKUP_FLAGS, game.ENTITY_TABLE, (annotator.TELEPORT, 12),
                  (annotator.ENEMY_DEFEATED, 16), (annotator.MISC_FLAGS, 8),
                  (annotator.BOSS_FLAGS, 2), (annotator.EVENT_FLAGS, 8),
                  (annotator.EQUIPMENT_STR, 2), (annotator.CAMERA_LOCK, 1)]
        start, length = EWRAM
        for address, size in ranges:
            with self.subTest(address=hex(address)):
                self.assertTrue(start <= address and address + size <= start + length)


if __name__ == "__main__":
    unittest.main()
