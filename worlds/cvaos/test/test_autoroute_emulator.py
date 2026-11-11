"""
The emulator interface of the automated routing search: the button check, the batched memory
helpers, and the fake emulator that the other unit tests run on.

These tests need no ROM and no emulator.

    python -m pytest worlds/cvaos/test/test_autoroute_emulator.py -v
"""
from __future__ import annotations

import unittest

from ..tools.autoroute.emulator import BUTTONS, FakeEmulator, check_buttons, read_many, write_many

ADDRESS = 0x02000088


class PlainEmulator:
    """An emulator with only the one-range read and write. It records each call it gets."""

    def __init__(self) -> None:
        self.fake = FakeEmulator()
        self.calls: list[tuple] = []

    def read(self, address: int, length: int) -> bytes:
        self.calls.append(("read", address, length))
        return self.fake.read(address, length)

    def write(self, address: int, data: bytes) -> None:
        self.calls.append(("write", address, bytes(data)))
        self.fake.write(address, data)


class BatchingEmulator(PlainEmulator):
    """An emulator that also has a batched read and a batched write of its own."""

    def read_many(self, ranges):
        self.calls.append(("read_many", ranges))
        return [self.fake.read(address, length) for address, length in ranges]

    def write_many(self, writes):
        self.calls.append(("write_many", writes))
        for address, data in writes:
            self.fake.write(address, data)


class TestCheckButtons(unittest.TestCase):
    def test_known_buttons_come_back_in_the_order_of_buttons(self):
        self.assertEqual(check_buttons(["Right", "A", "Start"]), ("A", "Start", "Right"))

    def test_every_button_is_accepted(self):
        self.assertEqual(check_buttons(reversed(BUTTONS)), BUTTONS)

    def test_a_button_named_twice_comes_back_once(self):
        self.assertEqual(check_buttons(["B", "Left", "B"]), ("B", "Left"))

    def test_no_buttons_give_an_empty_tuple(self):
        self.assertEqual(check_buttons([]), ())
        self.assertEqual(check_buttons(name for name in ()), ())

    def test_a_generator_of_names_is_accepted(self):
        self.assertEqual(check_buttons(name for name in ("Down", "L")), ("L", "Down"))

    def test_an_unknown_name_raises_value_error_that_names_it(self):
        with self.assertRaisesRegex(ValueError, "'Jump'"):
            check_buttons(["A", "Jump"])

    def test_names_are_case_sensitive(self):
        with self.assertRaises(ValueError):
            check_buttons(["a"])


class TestReadMany(unittest.TestCase):
    RANGES = ((ADDRESS + 2, 2), (ADDRESS, 1), (ADDRESS + 8, 3))

    def emulators(self) -> tuple[PlainEmulator, BatchingEmulator]:
        plain, batching = PlainEmulator(), BatchingEmulator()
        for emulator in (plain, batching):
            emulator.fake.write(ADDRESS, bytes(range(1, 11)))
        return plain, batching

    def test_an_emulator_without_a_batched_read_is_read_range_by_range_in_order(self):
        plain, _ = self.emulators()
        got = read_many(plain, self.RANGES)
        self.assertEqual(got, [b"\x03\x04", b"\x01", b"\x09\x0a\x00"])
        self.assertEqual(plain.calls, [("read", address, length)
                                       for address, length in self.RANGES])

    def test_an_emulator_with_a_batched_read_is_asked_once_for_every_range(self):
        _, batching = self.emulators()
        got = read_many(batching, (pair for pair in self.RANGES))
        self.assertEqual(got, [b"\x03\x04", b"\x01", b"\x09\x0a\x00"])
        self.assertEqual(batching.calls, [("read_many", list(self.RANGES))])

    def test_both_ways_give_the_same_bytes(self):
        plain, batching = self.emulators()
        self.assertEqual(read_many(plain, self.RANGES), read_many(batching, self.RANGES))

    def test_no_ranges_give_no_blocks(self):
        plain, _ = self.emulators()
        self.assertEqual(read_many(plain, []), [])
        self.assertEqual(plain.calls, [])


class TestWriteMany(unittest.TestCase):
    WRITES = ((ADDRESS, b"\x01\x02"), (ADDRESS + 1, b"\x07"), (ADDRESS + 4, b"\x05"))

    def test_an_emulator_without_a_batched_write_is_written_pair_by_pair_in_order(self):
        plain = PlainEmulator()
        write_many(plain, self.WRITES)
        self.assertEqual(plain.calls, [("write", address, data) for address, data in self.WRITES])
        # The later write to ADDRESS + 1 replaces the earlier one.
        self.assertEqual(plain.fake.read(ADDRESS, 5), b"\x01\x07\x00\x00\x05")

    def test_an_emulator_with_a_batched_write_is_given_every_pair_at_once(self):
        batching = BatchingEmulator()
        write_many(batching, (pair for pair in self.WRITES))
        self.assertEqual(batching.calls, [("write_many", list(self.WRITES))])
        self.assertEqual(batching.fake.read(ADDRESS, 5), b"\x01\x07\x00\x00\x05")


class TestFakeEmulatorMemory(unittest.TestCase):
    def test_memory_that_was_never_written_reads_as_zero(self):
        self.assertEqual(FakeEmulator().read(0x03000000, 4), bytes(4))

    def test_a_write_reads_back_and_leaves_its_neighbors_alone(self):
        fake = FakeEmulator()
        fake.write(ADDRESS + 1, b"\xff\x10")
        self.assertEqual(fake.read(ADDRESS, 4), b"\x00\xff\x10\x00")

    def test_a_read_of_no_bytes_is_empty(self):
        self.assertEqual(FakeEmulator().read(ADDRESS, 0), b"")


class TestFakeEmulatorFrames(unittest.TestCase):
    def test_each_frame_calls_on_frame_after_the_frame_count_goes_up(self):
        seen = []
        fake = FakeEmulator(on_frame=lambda emulator, held: seen.append((emulator.frame, held)))
        fake.step(["Left", "A"], 3)
        self.assertEqual(fake.frame, 3)
        self.assertEqual(seen, [(1, ("A", "Left")), (2, ("A", "Left")), (3, ("A", "Left"))])

    def test_a_step_is_one_frame_with_no_buttons_by_default(self):
        seen = []
        fake = FakeEmulator(on_frame=lambda emulator, held: seen.append(held))
        fake.step()
        self.assertEqual((fake.frame, seen), (1, [()]))

    def test_on_frame_sees_memory_and_its_writes_stay(self):
        def count(emulator, held):
            emulator.write(ADDRESS, bytes([emulator.read(ADDRESS, 1)[0] + 1]))

        fake = FakeEmulator(on_frame=count)
        fake.write(ADDRESS, b"\x05")
        fake.step(frames=4)
        self.assertEqual(fake.read(ADDRESS, 1), b"\x09")

    def test_zero_frames_run_nothing(self):
        seen = []
        fake = FakeEmulator(on_frame=lambda emulator, held: seen.append(held))
        fake.step(["A"], 0)
        self.assertEqual((fake.frame, seen), (0, []))

    def test_a_negative_frame_count_raises_value_error_and_runs_nothing(self):
        seen = []
        fake = FakeEmulator(on_frame=lambda emulator, held: seen.append(held))
        with self.assertRaises(ValueError):
            fake.step([], -1)
        self.assertEqual((fake.frame, seen), (0, []))

    def test_an_unknown_button_raises_value_error_and_runs_nothing(self):
        seen = []
        fake = FakeEmulator(on_frame=lambda emulator, held: seen.append(held))
        with self.assertRaises(ValueError):
            fake.step(["A", "Turbo"], 2)
        self.assertEqual((fake.frame, seen), (0, []))


class TestFakeEmulatorStates(unittest.TestCase):
    def test_load_returns_memory_and_the_frame_count_to_the_saved_state(self):
        fake = FakeEmulator()
        fake.write(ADDRESS, b"\x01")
        fake.step(frames=2)
        state = fake.save()
        fake.write(ADDRESS, b"\x02\x03")
        fake.step(frames=5)
        fake.load(state)
        self.assertEqual((fake.read(ADDRESS, 2), fake.frame), (b"\x01\x00", 2))

    def test_a_state_can_be_loaded_again(self):
        fake = FakeEmulator()
        fake.write(ADDRESS, b"\x01")
        state = fake.save()
        fake.load(state)
        fake.write(ADDRESS, b"\x09")
        fake.load(state)
        self.assertEqual(fake.read(ADDRESS, 1), b"\x01")

    def test_each_save_gets_its_own_handle(self):
        fake = FakeEmulator()
        first = fake.save()
        fake.write(ADDRESS, b"\x04")
        second = fake.save()
        self.assertNotEqual(first, second)
        fake.load(first)
        self.assertEqual(fake.read(ADDRESS, 1), b"\x00")
        fake.load(second)
        self.assertEqual(fake.read(ADDRESS, 1), b"\x04")

    def test_a_dropped_state_cannot_be_loaded(self):
        fake = FakeEmulator()
        state = fake.save()
        fake.drop(state)
        with self.assertRaises(KeyError):
            fake.load(state)

    def test_dropping_one_state_keeps_the_others(self):
        fake = FakeEmulator()
        first = fake.save()
        fake.write(ADDRESS, b"\x06")
        second = fake.save()
        fake.drop(first)
        fake.write(ADDRESS, b"\x00")
        fake.load(second)
        self.assertEqual(fake.read(ADDRESS, 1), b"\x06")


class TestFakeEmulatorReset(unittest.TestCase):
    def test_on_reset_runs_once_when_the_fake_is_made_and_once_for_each_reset(self):
        calls = []
        fake = FakeEmulator(on_reset=lambda emulator: calls.append(emulator.frame))
        self.assertEqual(calls, [0])
        fake.step(frames=3)
        fake.reset()
        self.assertEqual(calls, [0, 0])

    def test_reset_clears_memory_the_frame_count_and_every_state(self):
        fake = FakeEmulator()
        fake.write(ADDRESS, b"\x07")
        fake.step(frames=3)
        state = fake.save()
        fake.reset()
        self.assertEqual((fake.read(ADDRESS, 1), fake.frame), (b"\x00", 0))
        with self.assertRaises(KeyError):
            fake.load(state)

    def test_on_reset_can_set_memory_up_after_the_reset_clears_it(self):
        fake = FakeEmulator(on_reset=lambda emulator: emulator.write(ADDRESS, b"\x04"))
        fake.write(ADDRESS + 1, b"\x09")
        fake.reset()
        self.assertEqual(fake.read(ADDRESS, 2), b"\x04\x00")


if __name__ == "__main__":
    unittest.main()
