"""
Entry states: how an arrival by a door ends, the entry file, and the shipped entries.

``arrive`` runs on the fake game of ``test_autoroute_game``, with a hand-built room in place of the
ROM's and the landing functions replaced, so that a test script decides how Soma moves after the
warp. The entry-file tests write to a temporary folder. The shipped-file tests read
``entry_states.json``, and those that compare it with the ROM's doors and landings skip when the
USA ROM is not found.

    python -m pytest worlds/cvaos/test/test_autoroute_entries.py -v
"""
from __future__ import annotations

import itertools
import os
import struct
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

from ..data import entrance_info
from ..tools.annotator import emulator as annotator
from ..tools.annotator import landing
from ..tools.autoroute import entries, success
from ..tools.autoroute.emulator import FakeEmulator
from ..tools.autoroute.entries import (ENCHANTS, LIMIT, OUTCOMES, SETTLE, Entry, entries_text,
                                       equipped_enchant, load_entries, stored)
from ..tools.autoroute.game import GameError, Session
from ..tools.autoroute.traces import Recording
from .test_autoroute_game import (ARRIVED, MAX_HP, ROOM_A, fake_game, put_soma, read_rom, set_hp,
                                  soma_at, u16)

ROOM_B = 0x08520000
DOOR = "A00:A01 (upper)"
UP = landing.Exit(DOOR, ROOM_B, "top", (0, 48, 512, 48))
DOWN = landing.Exit("A00:A01 (lower)", ROOM_B, "bottom", (0, 464, 512, 464))
TARGET = success.Room("A00", ROOM_A, (UP, DOWN), (), (), ((annotator.EVENT_FLAGS, 0x20),), None)
ARENA = success.Room("A00", ROOM_A, (UP, DOWN), (), (), (), 11)
# The landing puts Soma's feet at (100, 100): camera y 0 is a screen top row of 48.
ARGS = {"room_address": ROOM_A, "camera_x": 0, "camera_y": 0, "soma_x": 100, "soma_y": 52,
        "feet_x": 100, "feet_y": 100}
START = (100, 100)


def frames_since_arrival(fake: FakeEmulator) -> int:
    return u16(fake, ARRIVED) - 1


def move(fake: FakeEmulator, dx: int, dy: int) -> None:
    x, y = soma_at(fake)
    put_soma(fake, x + dx, y + dy)


class TestEquippedEnchant(unittest.TestCase):
    def test_both_enchant_souls_give_wwalk_as_the_annotators_loadout_equips(self):
        for tokens in itertools.permutations(["Dive", "WWalk", "DJump"]):
            with self.subTest(tokens=tokens):
                self.assertEqual(equipped_enchant(tokens), "WWalk")
                writes = dict(annotator.loadout_writes((bytes(13), bytes(18), bytes(3)), tokens))
                equipped = writes[annotator.EQUIPPED_YELLOW][0]
                self.assertEqual(equipped, annotator.YELLOW_SOULS["WWalk"] + 1)

    def test_the_enchant_matches_the_annotators_loadout_for_every_choice(self):
        names = {index + 1: name for name, index in annotator.YELLOW_SOULS.items()}
        names[0] = ""
        for count in range(4):
            for tokens in itertools.combinations(["Dive", "WWalk", "Glide", "Slide"], count):
                with self.subTest(tokens=tokens):
                    writes = dict(annotator.loadout_writes((bytes(13), bytes(18), bytes(3)),
                                                           tokens))
                    self.assertEqual(equipped_enchant(iter(tokens)),
                                     names[writes[annotator.EQUIPPED_YELLOW][0]])

    def test_no_enchant_soul_gives_an_empty_string(self):
        self.assertEqual(equipped_enchant([]), "")
        self.assertEqual(equipped_enchant(["DJump", "Bat"]), "")

    def test_the_enchants_are_none_and_then_each_enchant_soul(self):
        self.assertEqual(ENCHANTS, ("", "WWalk", "Dive"))


class TestArrive(unittest.TestCase):
    """Arrivals on the fake game, with ``after(fake, n)`` playing frame n after the warp."""

    def arrive(self, after=None, tokens=(), boss=False, target=TARGET, flags=(), session=None):
        if session is None:
            session = Session(fake_game(after=after), rom=b"a ROM")
            session.boot()
        with mock.patch.object(success, "room", return_value=target) as room, \
                mock.patch.object(landing, "teleport_args", return_value=dict(ARGS)), \
                mock.patch.object(landing, "door_flags", return_value=list(flags)):
            entry = entries.arrive(session, DOOR, boss, tokens)
        room.assert_called_once_with("A00", session.rom)
        return entry, session

    def test_an_arrival_that_stops_moving_settles_with_the_frame_of_its_last_change(self):
        def falls(fake, n):
            if n <= 10:
                move(fake, 0, 1)

        entry, session = self.arrive(falls)
        self.assertEqual(entry, Entry(DOOR, False, "", "settles", 100, 110, 10))
        # The call returns SETTLE frames after the last change, with Soma at rest.
        self.assertEqual(frames_since_arrival(session.emulator), 10 + SETTLE)
        self.assertEqual(session.state().soma, (100, 110))

    def test_an_arrival_that_never_moves_settles_after_settle_frames(self):
        entry, session = self.arrive()
        self.assertEqual(entry, Entry(DOOR, False, "", "settles", *START, 0))
        self.assertEqual(frames_since_arrival(session.emulator), SETTLE)

    def test_an_arrival_that_leaves_is_placed_on_the_last_frame_of_normal_play(self):
        def floats_out(fake, n):
            if n <= 5:
                move(fake, 0, -1)
            elif n == 6:
                # The door transition moves Soma before the room changes.
                fake.write(annotator.MENU_STATE, b"\x02")
                put_soma(fake, 100, 460)
            elif n == 7:
                fake.write(annotator.ROOM_POINTER, struct.pack("<I", ROOM_B))
                fake.write(annotator.MENU_STATE, b"\x01")

        entry, _ = self.arrive(floats_out)
        self.assertEqual(entry, Entry(DOOR, False, "", "leaves", 100, 95, 7, False, DOOR))

    def test_an_arrival_that_leaves_the_game_leaves_by_no_door(self):
        def quits(fake, n):
            if n <= 2:
                move(fake, 3, 0)
            else:
                fake.write(annotator.GAME_STATE, b"\x00")

        entry, _ = self.arrive(quits)
        self.assertEqual(entry, Entry(DOOR, False, "", "leaves", 106, 100, 3, False, None))

    def test_an_arrival_still_moving_after_limit_frames_is_unsettled(self):
        entry, session = self.arrive(lambda fake, n: move(fake, 1, 0))
        self.assertEqual(entry, Entry(DOOR, False, "", "unsettled", 100 + LIMIT, 100, LIMIT))
        self.assertEqual(frames_since_arrival(session.emulator), LIMIT)

    def test_an_arrival_that_loses_hp_is_hurt(self):
        def hit(fake, n):
            if n <= 5:
                move(fake, 0, 1)
            if n == 3:
                set_hp(fake, MAX_HP - 20)

        entry, _ = self.arrive(hit)
        self.assertEqual(entry, Entry(DOOR, False, "", "settles", 100, 105, 5, True))

    def test_hp_that_rises_is_not_hurt(self):
        fake = fake_game(after=lambda fake, n: set_hp(fake, 250) if n == 3 else None)
        session = Session(fake, rom=b"a ROM")
        session.boot()
        set_hp(fake, 200)
        entry, _ = self.arrive(session=session)
        self.assertEqual((entry.outcome, entry.frames, entry.hurt), ("settles", 3, False))

    def test_arrive_prepares_the_room_sets_the_loadout_and_the_door_flags_before_the_warp(self):
        recording = Recording(fake_game())
        session = Session(recording, rom=b"a ROM")
        session.boot()
        flags = [(annotator.MISC_FLAGS, 35, True)]
        entry, _ = self.arrive(tokens=["DJump", "Dive"], flags=flags, session=session)
        self.assertEqual(entry.enchant, "Dive")
        fake = recording.emulator
        self.assertEqual(fake.read(annotator.EVENT_FLAGS + 4, 1), b"\x01")
        self.assertEqual(fake.read(annotator.MISC_FLAGS + 4, 1), b"\x08")
        self.assertEqual(session.state().used, {"DJump", "Dive"})
        addresses = [op[1] for op in recording.ops if op[0] == "write"]
        warp = addresses.index(f"{annotator.MENU_STATE:#010x}")
        for address in (annotator.EVENT_FLAGS + 4, annotator.EQUIPPED_YELLOW,
                        annotator.MISC_FLAGS + 4):
            with self.subTest(address=hex(address)):
                self.assertLess(addresses.index(f"{address:#010x}"), warp)

    def test_an_arena_entered_with_its_boss_clears_the_boss_flag(self):
        entry, session = self.arrive(boss=True, target=ARENA)
        self.assertTrue(entry.boss)
        self.assertEqual(session.emulator.read(annotator.BOSS_FLAGS + 1, 1), b"\x00")
        entry, session = self.arrive(boss=False, target=ARENA)
        self.assertFalse(entry.boss)
        self.assertEqual(session.emulator.read(annotator.BOSS_FLAGS + 1, 1), b"\x08")

    def test_arrive_raises_game_error_outside_normal_play(self):
        session = Session(fake_game(boot_frames=10_000), rom=b"a ROM")
        session.emulator.reset()
        with self.assertRaises(GameError):
            self.arrive(session=session)


# --- The entry file -------------------------------------------------------------------------------

PLAIN = Entry("009:008", False, "", "settles", 64, 175, 0)
PLAIN_DIVE = replace(PLAIN, enchant="Dive")
WATER = Entry("71E:720", False, "", "settles", 224, 400, 30)
WATER_DIVE = Entry("71E:720", False, "Dive", "settles", 224, 415, 0)
LEAVES = Entry("608:61D", False, "", "leaves", 384, 48, 34, False, "608:61D")
SHAKEN = Entry("619:616", False, "", "unsettled", 183, 339, LIMIT, True)
BOSS = Entry("00A:009", True, "", "settles", 40, 183, 20)
ONLY_WWALK = Entry("305:304", False, "WWalk", "settles", 16, 159, 0)
EVERY = [WATER_DIVE, SHAKEN, PLAIN_DIVE, WATER, LEAVES, PLAIN, BOSS, ONLY_WWALK]

TEXT = "\n".join([
    "[",
    '{"door": "009:008", "outcome": "settles", "x": 64, "y": 175, "frames": 0},',
    '{"door": "00A:009", "boss": true, "outcome": "settles", "x": 40, "y": 183, "frames": 20},',
    '{"door": "305:304", "enchant": "WWalk", "outcome": "settles", "x": 16, "y": 159,'
    ' "frames": 0},',
    '{"door": "608:61D", "outcome": "leaves", "x": 384, "y": 48, "frames": 34,'
    ' "left_by": "608:61D"},',
    '{"door": "619:616", "outcome": "unsettled", "x": 183, "y": 339, "frames": 420,'
    ' "hurt": true},',
    '{"door": "71E:720", "outcome": "settles", "x": 224, "y": 400, "frames": 30},',
    '{"door": "71E:720", "enchant": "Dive", "outcome": "settles", "x": 224, "y": 415,'
    ' "frames": 0}',
    "]",
    ""])


class TestEntryFile(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = os.path.join(folder.name, "entries.json")

    def written(self, entries_given) -> dict:
        with open(self.path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(entries_text(entries_given))
        return load_entries(self.path)

    def test_the_text_leaves_out_default_fields_and_repeated_enchant_rows_in_sorted_order(self):
        self.assertEqual(entries_text(EVERY), TEXT)

    def test_an_enchant_row_that_ends_as_the_plain_row_is_left_out(self):
        self.assertNotIn("Dive", entries_text([PLAIN, PLAIN_DIVE]))
        self.assertIn('"enchant": "Dive"', entries_text([PLAIN_DIVE]))

    def test_an_enchant_row_that_differs_from_the_plain_row_in_any_field_is_kept(self):
        for change in ({"x": 65}, {"frames": 1}, {"hurt": True}, {"outcome": "unsettled"}):
            with self.subTest(change=change):
                self.assertIn('"enchant": "Dive"',
                              entries_text([PLAIN, replace(PLAIN_DIVE, **change)]))

    def test_the_file_reads_back_as_the_entries_it_kept(self):
        table = self.written(EVERY)
        self.assertEqual(table, {entry.key: entry for entry in EVERY if entry is not PLAIN_DIVE})

    def test_stored_gives_back_every_entry_including_the_left_out_ones(self):
        table = self.written(EVERY)
        with mock.patch.object(entries, "_stored", return_value=table):
            for entry in EVERY:
                tokens = [entry.enchant] if entry.enchant else []
                with self.subTest(entry=entry.key):
                    self.assertEqual(stored(entry.door, entry.boss, tokens), entry)

    def test_stored_falls_back_to_the_plain_row_with_the_enchant_filled_in(self):
        table = {entry.key: entry for entry in (PLAIN, WATER, WATER_DIVE)}
        with mock.patch.object(entries, "_stored", return_value=table):
            self.assertEqual(stored("009:008", tokens=["Dive"]), PLAIN_DIVE)
            self.assertEqual(stored("71E:720", tokens=["Dive"]), WATER_DIVE)
            # With both enchant souls WWalk is equipped, and the file has no WWalk row.
            self.assertEqual(stored("71E:720", tokens=["Dive", "WWalk"]),
                             replace(WATER, enchant="WWalk"))
            self.assertEqual(stored("71E:720", tokens=["DJump"]), WATER)

    def test_stored_is_none_without_a_row_to_give(self):
        table = {entry.key: entry for entry in (ONLY_WWALK, BOSS)}
        with mock.patch.object(entries, "_stored", return_value=table):
            self.assertIsNone(stored("305:304"))
            self.assertIsNone(stored("305:304", tokens=["Dive"]))
            self.assertIsNone(stored("00A:009"))
            self.assertIsNone(stored("FFF:000"))
            self.assertEqual(stored("00A:009", boss=True), BOSS)

    def test_an_empty_file_holds_no_entries(self):
        self.assertEqual(self.written([]), {})


# --- The shipped entries --------------------------------------------------------------------------

class TestShippedEntries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.table = load_entries()

    def test_the_file_reads_back_byte_for_byte(self):
        with open(entries.entry_path(), "rb") as handle:
            raw = handle.read()
        self.assertEqual(entries_text(self.table.values()).encode("utf-8"), raw)

    def test_every_door_has_a_plain_entry_and_every_entry_names_a_door(self):
        doors = set(entrance_info.by_door_identifier_unique)
        plain = {door for door, boss, enchant in self.table if not boss and not enchant}
        self.assertEqual(plain, doors)
        self.assertLessEqual({door for door, _, _ in self.table}, doors)

    def test_outcomes_and_enchants_are_the_known_words(self):
        for entry in self.table.values():
            with self.subTest(entry=entry.key):
                self.assertIn(entry.outcome, OUTCOMES)
                self.assertIn(entry.enchant, ENCHANTS)

    def test_only_an_entry_that_leaves_names_the_door_it_left_by(self):
        for entry in self.table.values():
            with self.subTest(entry=entry.key):
                if entry.left_by is not None:
                    self.assertEqual(entry.outcome, "leaves")
                    self.assertIn(entry.left_by, entrance_info.by_door_identifier_unique)

    def test_only_an_unsettled_entry_has_limit_frames(self):
        for entry in self.table.values():
            with self.subTest(entry=entry.key):
                self.assertEqual(entry.frames == LIMIT, entry.outcome == "unsettled")
                self.assertTrue(0 <= entry.frames <= LIMIT)

    def test_stored_reads_the_shipped_file(self):
        self.assertEqual(stored("71E:720", tokens=["Dive"]).enchant, "Dive")
        plain = stored("009:008")
        self.assertEqual(plain.key, ("009:008", False, ""))
        self.assertEqual(stored("009:008", tokens=["Dive"]), replace(plain, enchant="Dive"))


class TestShippedEntriesOnTheRom(unittest.TestCase):
    # Doors whose landing falls onto a floor that ends within reach of Soma's feet.
    PLATFORM_EDGES = ("10B:108", "B05:B24", "20E:201", "804:803", "A05:A03", "90D:90C")
    # Doors whose landing is beside a wall that door_flags opens, as measured in the game.
    WALLS = ("305:301", "023:00B", "604:61B", "70A:709", "30D:310")
    # The outside boss doors, whose boss's bit door_flags sets.
    BOSS_DOORS = ("009:00A", "023:00A", "102:106", "105:106", "10A:106", "210:20F", "30F:313",
                  "406:40B", "40E:40B", "613:607", "724:725", "805:801", "901:904", "B0D:B0B")

    @classmethod
    def setUpClass(cls):
        cls.rom = read_rom()
        cls.table = load_entries()

    def assert_exact(self, entry: Entry):
        self.assertEqual(entry.outcome, "settles", entry)
        dx, dy = entries.offset(entry, self.rom)
        self.assertEqual(dx, 0, entry)
        self.assertLessEqual(abs(dy), 1, entry)

    def test_arena_entries_are_only_for_rooms_with_a_boss_bit(self):
        for door, boss, _ in self.table:
            if boss:
                with self.subTest(door=door):
                    self.assertIsNotNone(success.room(door.split(":")[0], self.rom).boss_bit)

    def test_the_file_has_an_entry_for_every_visit_and_for_nothing_else(self):
        found = {(door, boss) for door, boss, enchant in self.table if not enchant}
        self.assertEqual(found, set(entries.visits(rom=self.rom)))

    def test_visits_count_each_door_of_an_arena_twice(self):
        self.assertEqual(entries.visits(["00A"], self.rom),
                         [("00A:009", False), ("00A:023", False),
                          ("00A:009", True), ("00A:023", True)])
        self.assertEqual(entries.visits(["009"], self.rom),
                         [("009:008", False), ("009:006", False), ("009:00A", False)])

    def test_the_platform_edge_doors_settle_where_their_landing_says(self):
        for door in self.PLATFORM_EDGES:
            with self.subTest(door=door):
                self.assert_exact(stored(door))

    def test_the_wall_and_boss_doors_settle_where_their_landing_says(self):
        for door in self.WALLS + self.BOSS_DOORS:
            with self.subTest(door=door):
                self.assert_exact(stored(door))

    def test_the_wall_door_under_water_settles_at_its_landing_with_dive(self):
        # Without Dive the water lifts Soma above the landing at 71E:720.
        self.assert_exact(stored("71E:720", tokens=["Dive"]))

    def test_soma_stands_on_the_wall_that_door_flags_leaves_whole_at_109_108(self):
        self.assertEqual(entries.offset(stored("109:108"), self.rom), (0, -32))


if __name__ == "__main__":
    unittest.main()
