"""
The success tests of the automated routing search, and the watch that applies them in a room.

The tests of single functions build their own doors, pickups, enemies and entities. The watch
runs on a fake emulator whose memory a test writes directly. The ROM tests read the USA ROM for
real rooms, and skip when it is not found.

    python -m pytest worlds/cvaos/test/test_autoroute_success.py -v
"""
from __future__ import annotations

import struct
import unittest

from ..data import room_info
from ..tools.annotator import emulator as annotator
from ..tools.annotator import landing
from ..tools.annotator.testing import REACH_DISTANCE
from ..tools.autoroute import game, success
from ..tools.autoroute.benchmark import Question
from ..tools.autoroute.emulator import FakeEmulator
from ..tools.autoroute.game import Entity, Session
from ..tools.autoroute.success import (TOUCH_BELOW, TOUCH_SIDE, Enemy, Event, Pickup, Room, Watch,
                                       collected, defeated, dying, exit_door, prepare, reached,
                                       touching)
from ..tools.autoroute.traces import Recording
from .test_autoroute_game import entity_record, in_play, put_soma, read_rom, write_entities

ROOM = 0x08510000
NEIGHBOR = 0x08520000
FAR = 0x08530000
ELSEWHERE = 0x08540000

UPPER = landing.Exit("A00:A01 (upper)", NEIGHBOR, "left", (0, 48, 0, 256))
LOWER = landing.Exit("A00:A01 (lower)", NEIGHBOR, "left", (0, 256, 0, 464))
HIDDEN = landing.Exit("A00:A02", FAR, None, None)
EXITS = (UPPER, LOWER, HIDDEN)

COIN = Pickup(7, "Coin", 200, 150, 13, True)
HARD = Pickup(8, "Hard Potion", 400, 150, 20, False)
LOOSE = Pickup(9, "Loose Potion", 300, 400, None, True)
BAT = Enemy(100, "Bat", 0, 600, 300, 3, False, False)
MERMAN_A = Enemy(101, "Merman", 3, 700, 400, 5, True, False)
MERMAN_B = Enemy(102, "Merman", 3, 800, 400, 6, True, False)
PLAIN = Room("A00", ROOM, EXITS, (COIN, HARD, LOOSE), (BAT, MERMAN_A, MERMAN_B), (), None)
# Where Soma stands when nothing should happen: far from every target of PLAIN.
HOME = (120, 300)

SKULL = Enemy(27, "Creaking Skull", 20, 300, 180, 4, False, True)
SKELETON = Enemy(26, "Skeleton", 2, 128, 176, 3, False, False)
ARENA = Room("A10", ROOM, EXITS, (), (SKULL, SKELETON), ((annotator.EVENT_FLAGS, 0x20),), 11)

OWN, OTHER, CHILD = 0x1B, 0x1C, 0x60   # an own slot, another own slot, and a slot for a child


def entity(slot: int, type_id: int = 3, entry_id: int = 5, hp: int = 10,
           parent=None) -> Entity:
    return Entity(slot, type_id, entry_id, hp, 0, 0, parent)


def defeated_bit(fake: FakeEmulator, type_id: int) -> bool:
    return bool(fake.read(annotator.ENEMY_DEFEATED + (type_id >> 3), 1)[0] & (1 << (type_id & 7)))


def strike(fake: FakeEmulator, type_id: int) -> None:
    """Set the defeated bit of an enemy type, as the game does on a defeat."""
    Session(fake).set_flag(annotator.ENEMY_DEFEATED, type_id, True)


def watching(target: Room = PLAIN, boss: bool = False, soma=HOME):
    fake = FakeEmulator()
    in_play(fake, target.address, soma)
    recording = Recording(fake)
    return fake, recording, Watch(Session(recording), target, boss)


def writes(recording: Recording) -> list[list]:
    return [op for op in recording.ops if op[0] == "write"]


# --- exit_door ------------------------------------------------------------------------------------

class TestExitDoor(unittest.TestCase):
    def test_the_only_door_to_the_new_room_is_the_answer_wherever_soma_was(self):
        self.assertEqual(exit_door(EXITS, (700, 700), FAR), "A00:A02")

    def test_no_door_to_the_new_room_gives_none(self):
        self.assertIsNone(exit_door(EXITS, (0, 100), ELSEWHERE))

    def test_of_two_doors_to_one_room_the_one_whose_line_is_nearest_wins(self):
        self.assertEqual(exit_door(EXITS, (4, 100), NEIGHBOR), "A00:A01 (upper)")
        self.assertEqual(exit_door(EXITS, (4, 300), NEIGHBOR), "A00:A01 (lower)")

    def test_the_distance_is_to_the_nearest_point_of_a_line(self):
        # (20, 250) is 20 px from the upper line and 21 px from the end of the lower one.
        self.assertEqual(exit_door(EXITS, (20, 250), NEIGHBOR), "A00:A01 (upper)")
        self.assertEqual(exit_door(EXITS, (20, 262), NEIGHBOR), "A00:A01 (lower)")

    def test_a_door_without_a_line_is_not_chosen_between_two(self):
        exits = (landing.Exit("A00:A01 (upper)", NEIGHBOR, None, None), LOWER)
        self.assertEqual(exit_door(exits, (0, 60), NEIGHBOR), "A00:A01 (lower)")

    def test_two_doors_without_lines_give_none(self):
        exits = (landing.Exit("A00:A01 (upper)", NEIGHBOR, None, None),
                 landing.Exit("A00:A01 (lower)", NEIGHBOR, None, None))
        self.assertIsNone(exit_door(exits, (0, 60), NEIGHBOR))


class TestExitDoorOnTheRom(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = read_rom()

    def exits(self, room: str) -> list[landing.Exit]:
        return landing.exits(room_info.by_identifier[room].room_address, self.rom)

    def test_room_500s_two_doors_to_501_are_told_apart_by_where_soma_left(self):
        exits, new_room = self.exits("500"), room_info.by_identifier["501"].room_address
        self.assertEqual(exit_door(exits, (100, 460), new_room), "500:501 (left)")
        self.assertEqual(exit_door(exits, (400, 460), new_room), "500:501 (right)")

    def test_room_306s_two_doors_to_318_are_told_apart_by_height(self):
        exits, new_room = self.exits("306"), room_info.by_identifier["318"].room_address
        self.assertEqual(exit_door(exits, (16, 200), new_room), "306:318 (upper)")
        self.assertEqual(exit_door(exits, (16, 300), new_room), "306:318 (lower)")

    def test_the_two_exits_with_no_line_still_name_their_door(self):
        for room, door, neighbor in (("505", "505:50B", "50B"), ("901", "901:904", "904")):
            with self.subTest(door=door):
                exits = self.exits(room)
                lineless = [exit_.door for exit_ in exits if exit_.line is None]
                self.assertEqual(lineless, [door])
                new_room = room_info.by_identifier[neighbor].room_address
                self.assertEqual(exit_door(exits, (100, 100), new_room), door)


# --- Pickups and reaching -------------------------------------------------------------------------

class TestCollected(unittest.TestCase):
    def test_a_pickup_is_collected_when_its_bit_is_set(self):
        flags = bytearray(20)
        self.assertFalse(collected(bytes(flags), COIN))
        flags[1] = 0x20   # flag 13 is bit 5 of the second byte
        self.assertTrue(collected(bytes(flags), COIN))

    def test_other_bits_say_nothing_about_a_pickup(self):
        flags = bytearray(b"\xff" * 20)
        flags[1] = 0xFF ^ 0x20
        self.assertFalse(collected(bytes(flags), COIN))

    def test_a_pickup_without_a_flag_is_never_collected(self):
        self.assertFalse(collected(b"\xff" * 20, LOOSE))


class TestTouching(unittest.TestCase):
    LOW, HIGH = TOUCH_BELOW

    def test_soma_touches_a_pickup_with_his_feet_anywhere_in_the_box_ends_included(self):
        x, y = HARD.x, HARD.y
        for soma in ((x, y + self.LOW), (x, y + self.HIGH), (x - TOUCH_SIDE, y + self.LOW),
                     (x + TOUCH_SIDE, y + self.HIGH), (x + 1, (2 * y + self.LOW + self.HIGH) // 2)):
            with self.subTest(soma=soma):
                self.assertTrue(touching(soma, HARD))

    def test_one_pixel_outside_the_box_is_not_touching(self):
        x, y = HARD.x, HARD.y
        for soma in ((x - TOUCH_SIDE - 1, y + self.LOW), (x + TOUCH_SIDE + 1, y + self.LOW),
                     (x, y + self.LOW - 1), (x, y + self.HIGH + 1)):
            with self.subTest(soma=soma):
                self.assertFalse(touching(soma, HARD))

    def test_feet_level_with_the_pickup_or_above_it_are_not_touching(self):
        x, y = HARD.x, HARD.y
        for soma in ((x, y), (x, y - 1), (x, y - self.HIGH), (x + TOUCH_SIDE, y - self.LOW)):
            with self.subTest(soma=soma):
                self.assertFalse(touching(soma, HARD))

    def test_the_box_lies_wholly_below_the_pickup(self):
        self.assertTrue(0 < self.LOW <= self.HIGH)
        self.assertGreater(TOUCH_SIDE, 0)

    def test_the_box_lies_inside_the_range_in_which_the_game_collects(self):
        # The game collects with Soma's feet 7 to 47 px below a pickup, and from 14 px to its left
        # to 13 px to its right. A box that reached outside that range would report a touch where
        # the game collects nothing.
        self.assertTrue(7 <= self.LOW and self.HIGH <= 47)
        self.assertLessEqual(TOUCH_SIDE, 13)


class TestReached(unittest.TestCase):
    def test_soma_reaches_a_placement_within_the_annotators_reach_distance(self):
        self.assertEqual(REACH_DISTANCE, 40)
        self.assertTrue(reached((BAT.x + 24, BAT.y + 32), BAT))
        self.assertTrue(reached((BAT.x - 40, BAT.y), BAT))
        self.assertFalse(reached((BAT.x + 24, BAT.y + 33), BAT))
        self.assertFalse(reached((BAT.x, BAT.y - 41), BAT))


# --- Defeats --------------------------------------------------------------------------------------

class TestDying(unittest.TestCase):
    def test_the_placements_own_entity_below_zero_hp_counts(self):
        self.assertTrue(dying([entity(OWN, hp=-1)], MERMAN_A))

    def test_zero_hp_is_not_a_defeat(self):
        self.assertFalse(dying([entity(OWN, hp=0)], MERMAN_A))

    def test_a_living_own_entity_is_not_a_defeat(self):
        self.assertFalse(dying([entity(OWN, hp=5)], MERMAN_A))

    def test_another_placements_own_entity_does_not_count(self):
        entities = [entity(OWN, hp=5), entity(OTHER, entry_id=6, hp=-1)]
        self.assertFalse(dying(entities, MERMAN_A))
        self.assertTrue(dying(entities, MERMAN_B))

    def test_an_entity_outside_the_room_slots_is_not_the_placements_own(self):
        self.assertFalse(dying([entity(0x50, hp=-1)], MERMAN_A))
        self.assertFalse(dying([entity(0x1A, hp=-1)], MERMAN_A))

    def test_a_defeated_entity_that_records_the_own_entity_as_parent_counts(self):
        entities = [entity(OWN, hp=5), entity(CHILD, entry_id=0, hp=-1, parent=OWN)]
        self.assertTrue(dying(entities, MERMAN_A))

    def test_a_defeated_child_of_another_placement_does_not_count(self):
        entities = [entity(OWN, hp=5), entity(OTHER, entry_id=6, hp=5),
                    entity(CHILD, entry_id=0, hp=-1, parent=OTHER)]
        self.assertFalse(dying(entities, MERMAN_A))
        self.assertTrue(dying(entities, MERMAN_B))

    def test_a_defeated_entity_of_another_type_does_not_count(self):
        entities = [entity(OWN, hp=5), entity(CHILD, type_id=4, entry_id=0, hp=-1, parent=OWN)]
        self.assertFalse(dying(entities, MERMAN_A))

    def test_nothing_counts_while_the_own_entity_is_missing(self):
        entities = [entity(CHILD, entry_id=0, hp=-1, parent=OWN)]
        self.assertFalse(dying(entities, MERMAN_A))
        generator = Enemy(449, "Medusa Head", 57, 0, 0, 7, True, False)
        self.assertFalse(dying([entity(CHILD, type_id=57, entry_id=0, hp=-1)], generator))

    def test_an_unparented_defeat_counts_for_an_unlinked_generator_whose_entity_is_present(self):
        self.assertEqual(success.UNLINKED_GENERATORS, {449, 450})
        generator = Enemy(449, "Medusa Head", 57, 0, 0, 7, True, False)
        entities = [entity(OWN, type_id=57, entry_id=7, hp=1),
                    entity(CHILD, type_id=57, entry_id=0, hp=-1)]
        self.assertTrue(dying(entities, generator))

    def test_an_unparented_defeat_does_not_count_for_any_other_placement(self):
        entities = [entity(OWN, hp=5), entity(CHILD, entry_id=0, hp=-1)]
        self.assertFalse(dying(entities, MERMAN_A))

    def test_somas_slot_never_counts(self):
        entities = [entity(OWN, hp=5), entity(0, entry_id=0, hp=-1, parent=OWN)]
        self.assertFalse(dying(entities, MERMAN_A))

    def test_a_placement_with_no_entry_never_counts(self):
        unknown = Enemy(103, "Merman", 3, 900, 400, None, True, False)
        entities = [entity(OWN, entry_id=0, hp=-1), entity(CHILD, entry_id=0, hp=-1, parent=OWN)]
        self.assertFalse(dying(entities, unknown))

    def test_a_placement_of_entry_zero_takes_only_an_unparented_entity_as_its_own(self):
        first = Enemy(104, "Merman", 3, 64, 400, 0, True, False)
        self.assertTrue(dying([entity(OWN, entry_id=0, hp=-1)], first))
        # An entity with entry 0 that records a parent was made by another entity.
        self.assertFalse(dying([entity(OWN, entry_id=0, hp=-1, parent=0x30)], first))
        entities = [entity(OWN, entry_id=0, hp=5), entity(CHILD, entry_id=0, hp=-1, parent=OWN)]
        self.assertTrue(dying(entities, first))


class TestDefeated(unittest.TestCase):
    def test_a_single_placement_is_defeated_while_its_types_bit_is_set(self):
        bits = bytearray(16)
        self.assertFalse(defeated([], bytes(bits), BAT))
        bits[0] = 0x01
        self.assertTrue(defeated([], bytes(bits), BAT))
        self.assertTrue(defeated([entity(OWN, type_id=0, entry_id=3, hp=5)], bytes(bits), BAT))

    def test_a_placement_with_twins_is_decided_by_the_entities(self):
        bits = b"\x08" + bytes(15)   # type 3 is bit 3 of the first byte
        self.assertFalse(defeated([entity(OWN, hp=5)], bits, MERMAN_A))
        self.assertTrue(defeated([entity(OWN, hp=-1)], bytes(16), MERMAN_A))


# --- Rooms ----------------------------------------------------------------------------------------

class TestRoomPresent(unittest.TestCase):
    def test_an_ordinary_room_presents_every_enemy_whatever_boss_says(self):
        self.assertEqual(PLAIN.present(), PLAIN.enemies)
        self.assertEqual(PLAIN.present(True), PLAIN.enemies)

    def test_an_arena_presents_its_other_enemies_by_default_and_its_boss_with_boss(self):
        self.assertEqual(ARENA.present(), (SKELETON,))
        self.assertEqual(ARENA.present(True), (SKULL,))


class TestRoomOnTheRom(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = read_rom()

    def enemies(self, room: str) -> dict[int, Enemy]:
        return {enemy.number: enemy for enemy in success.room(room, self.rom).enemies}

    def test_two_placements_at_one_spot_take_the_entries_in_number_order(self):
        heads = self.enemies("615")
        self.assertEqual((heads[449].entry_id, heads[450].entry_id), (7, 8))
        self.assertEqual((heads[449].x, heads[449].y), (heads[450].x, heads[450].y))
        mermen = self.enemies("72D")
        self.assertEqual((mermen[638].entry_id, mermen[639].entry_id), (11, 12))
        self.assertEqual((mermen[638].x, mermen[638].y), (mermen[639].x, mermen[639].y))
        self.assertTrue(all(enemy.twins for enemy in (heads[449], mermen[638])))

    def test_an_arena_holds_its_boss_until_the_flag_is_set(self):
        arena = success.room("00A", self.rom)
        self.assertEqual(arena.boss_bit, 11)
        self.assertEqual([enemy.name for enemy in arena.present(True)], ["Creaking Skull"])
        self.assertEqual({enemy.name for enemy in arena.present()}, {"Skeleton"})

    def test_a_room_whose_enemies_wait_for_another_rooms_boss_sets_that_bosss_flag(self):
        room = success.room("710", self.rom)
        self.assertIsNone(room.boss_bit)
        self.assertIn((annotator.BOSS_FLAGS, success.room("725", self.rom).boss_bit), room.flags)

    def test_a_pickup_that_only_hard_mode_places_does_not_spawn(self):
        pickups = {pickup.number: pickup for pickup in success.room("901", self.rom).pickups}
        self.assertFalse(pickups[109].spawns)
        self.assertEqual(pickups[109].flag, 70)

    def test_an_identifier_that_names_no_room_raises_key_error(self):
        with self.assertRaises(KeyError):
            success.room("FFF", self.rom)


# --- prepare --------------------------------------------------------------------------------------

class TestPrepare(unittest.TestCase):
    ROOM = Room("A00", ROOM, EXITS, (COIN, LOOSE), (BAT, MERMAN_A),
                ((annotator.EVENT_FLAGS, 0x20), (annotator.BOSS_FLAGS, 5)), 11)

    def fake(self, boss_flags: bytes = b"\x00\x00") -> FakeEmulator:
        fake = FakeEmulator()
        fake.write(game.PICKUP_FLAGS[0], b"\xff" * 20)
        fake.write(annotator.ENEMY_DEFEATED, b"\xff" * 16)
        fake.write(annotator.BOSS_FLAGS, boss_flags)
        return fake

    def test_prepare_clears_the_rooms_pickups_and_defeated_bits_and_sets_its_flags(self):
        fake = self.fake()
        prepare(Session(fake), self.ROOM)
        self.assertEqual(fake.read(game.PICKUP_FLAGS[0], 20), b"\xff\xdf" + b"\xff" * 18)
        # Bat is type 0 and Merman type 3, both in the first byte.
        self.assertEqual(fake.read(annotator.ENEMY_DEFEATED, 16), b"\xf6" + b"\xff" * 15)
        self.assertEqual(fake.read(annotator.EVENT_FLAGS, 8), bytes(4) + b"\x01" + bytes(3))

    def test_by_default_the_arenas_boss_flag_is_set(self):
        fake = self.fake()
        prepare(Session(fake), self.ROOM)
        # Bit 5 from the room's flags, and bit 11 of the arena's own boss.
        self.assertEqual(fake.read(annotator.BOSS_FLAGS, 2), b"\x20\x08")

    def test_with_boss_the_arenas_boss_flag_is_cleared(self):
        fake = self.fake(b"\xff\xff")
        prepare(Session(fake), self.ROOM, boss=True)
        self.assertEqual(fake.read(annotator.BOSS_FLAGS, 2), b"\xff\xf7")

    def test_outside_an_arena_the_boss_flags_keep_their_other_bits(self):
        room = Room("A00", ROOM, EXITS, (), (), (), None)
        for boss in (False, True):
            with self.subTest(boss=boss):
                fake = self.fake(b"\x00\x08")
                prepare(Session(fake), room, boss)
                self.assertEqual(fake.read(annotator.BOSS_FLAGS, 2), b"\x00\x08")


# --- Watch ----------------------------------------------------------------------------------------

class TestEvent(unittest.TestCase):
    def test_an_event_gives_the_key_of_the_question_it_answers(self):
        question = Question("enemy", "A00", "A00:A01 (upper)", "100", "Bat", ())
        event = Event("enemy", "100", "reached")
        self.assertEqual(event.key("A00:A01 (upper)"), question.key)
        self.assertEqual(Event("door", "A00:A02", "left").key("A00:A01 (lower)"),
                         "door|A00:A01 (lower)|A00:A02")


class TestWatchLeaving(unittest.TestCase):
    def test_the_door_is_found_from_the_last_frame_of_normal_play(self):
        fake, _, watch = watching(soma=(30, 100))
        self.assertEqual(watch.look(), [])
        # The door transition moves Soma toward the lower door before the room changes.
        fake.write(annotator.MENU_STATE, b"\x02")
        put_soma(fake, 30, 400)
        self.assertEqual(watch.look(), [])
        self.assertIsNone(watch.over)
        in_play(fake, NEIGHBOR, (900, 300))
        self.assertEqual(watch.look(), [Event("door", "A00:A01 (upper)", "left")])
        self.assertEqual(watch.over, "left")

    def test_leaving_in_a_way_that_no_door_explains_is_gone(self):
        fake, _, watch = watching()
        watch.look()
        in_play(fake, ELSEWHERE, (100, 100))
        self.assertEqual(watch.look(), [])
        self.assertEqual(watch.over, "gone")

    def test_leaving_before_soma_was_seen_in_normal_play_is_gone(self):
        fake, _, watch = watching(soma=(30, 100))
        fake.write(annotator.MENU_STATE, b"\x02")
        watch.look()
        in_play(fake, NEIGHBOR, (900, 300))
        self.assertEqual(watch.look(), [])
        self.assertEqual(watch.over, "gone")

    def test_a_game_that_is_no_longer_in_game_ends_the_watch(self):
        fake, _, watch = watching()
        fake.write(annotator.GAME_STATE, b"\x02")
        self.assertEqual(watch.look(), [])
        self.assertEqual(watch.over, "gone")

    def test_after_the_watch_ends_a_look_reads_nothing_and_reports_nothing(self):
        fake, recording, watch = watching(soma=(30, 100))
        watch.look()
        in_play(fake, NEIGHBOR, (900, 300))
        watch.look()
        count = len(recording.ops)
        in_play(fake, ROOM, (COIN.x, COIN.y))
        self.assertEqual(watch.look(), [])
        self.assertEqual(len(recording.ops), count)

    def test_the_state_of_the_last_look_is_kept(self):
        _, _, watch = watching()
        self.assertIsNone(watch.state)
        watch.look()
        self.assertEqual((watch.state.room_pointer, watch.state.soma), (ROOM, HOME))


class TestWatchPickupsAndReaching(unittest.TestCase):
    def test_a_collected_pickup_is_reported_once(self):
        fake, _, watch = watching()
        Session(fake).set_flag(game.PICKUP_FLAGS[0], COIN.flag, True)
        self.assertEqual(watch.look(), [Event("pickup", "7", "collected")])
        self.assertEqual(watch.look(), [])
        self.assertEqual(watch.events, [Event("pickup", "7", "collected")])

    def test_the_pickup_flags_are_read_only_while_a_pickup_with_a_flag_is_uncollected(self):
        only_coin = Room("A00", ROOM, EXITS, (COIN, LOOSE), (), (), None)
        fake, recording, watch = watching(only_coin)
        flags = f"{game.PICKUP_FLAGS[0]:#010x}"
        watch.look()
        self.assertIn(flags, [op[1] for op in recording.ops if op[0] == "read"])
        Session(fake).set_flag(game.PICKUP_FLAGS[0], COIN.flag, True)
        watch.look()
        count = len(recording.ops)
        watch.look()
        self.assertNotIn(flags, [op[1] for op in recording.ops[count:] if op[0] == "read"])

    def test_a_pickup_that_does_not_spawn_is_touched(self):
        _, _, watch = watching(soma=(HARD.x + TOUCH_SIDE, HARD.y + TOUCH_BELOW[1]))
        self.assertEqual(watch.look(), [Event("pickup", "8", "touched")])
        self.assertEqual(watch.look(), [])

    def test_a_pickup_that_spawns_is_never_touched(self):
        _, _, watch = watching(soma=(COIN.x, COIN.y + TOUCH_BELOW[0]))
        self.assertTrue(touching((COIN.x, COIN.y + TOUCH_BELOW[0]), COIN))
        self.assertEqual(watch.look(), [])

    def test_reaching_an_enemy_is_reported_with_its_number_as_text(self):
        fake, _, watch = watching()
        put_soma(fake, BAT.x + 30, BAT.y)
        self.assertEqual(watch.look(), [Event("enemy", "100", "reached")])
        self.assertEqual([event.key("A00:A01 (upper)") for event in watch.events],
                         ["enemy|A00:A01 (upper)|100"])

    def test_an_arena_watched_with_its_boss_looks_only_for_the_boss(self):
        fake, _, watch = watching(ARENA, boss=True)
        self.assertEqual(watch.enemies, (SKULL,))
        put_soma(fake, SKELETON.x, SKELETON.y)
        self.assertEqual(watch.look(), [])
        put_soma(fake, SKULL.x, SKULL.y)
        self.assertEqual(watch.look(), [Event("enemy", "27", "reached")])

    def test_nothing_is_reported_on_a_frame_outside_normal_play(self):
        fake, _, watch = watching()
        fake.write(annotator.MENU_STATE, b"\x03")
        put_soma(fake, BAT.x, BAT.y)
        Session(fake).set_flag(game.PICKUP_FLAGS[0], COIN.flag, True)
        self.assertEqual(watch.look(), [])
        self.assertIsNone(watch.over)


class TestWatchDefeats(unittest.TestCase):
    def test_a_single_placement_is_credited_by_its_bit_and_the_bit_stays_set(self):
        fake, recording, watch = watching()
        strike(fake, BAT.type_id)
        self.assertEqual(watch.look(), [Event("enemy", "100", "defeated")])
        self.assertTrue(defeated_bit(fake, BAT.type_id))
        self.assertEqual(writes(recording), [])

    def test_a_twin_is_credited_from_the_table_and_the_bit_is_cleared(self):
        fake, recording, watch = watching()
        write_entities(fake, {OWN: entity_record(3, 5, -1, (0, 0)),
                              OTHER: entity_record(3, 6, 10, (0, 0))})
        strike(fake, 3)
        self.assertEqual(watch.look(), [Event("enemy", "101", "defeated")])
        self.assertFalse(defeated_bit(fake, 3))
        self.assertEqual([op[1] for op in writes(recording)],
                         [f"{annotator.ENEMY_DEFEATED:#010x}"])

    def test_the_bit_stays_set_once_every_twin_is_credited(self):
        fake, _, watch = watching()
        write_entities(fake, {OWN: entity_record(3, 5, -1, (0, 0)),
                              OTHER: entity_record(3, 6, 10, (0, 0))})
        strike(fake, 3)
        watch.look()
        write_entities(fake, {OWN: bytes(game.ENTITY_SIZE),
                              OTHER: entity_record(3, 6, -4, (0, 0))})
        strike(fake, 3)
        self.assertEqual(watch.look(), [Event("enemy", "102", "defeated")])
        self.assertTrue(defeated_bit(fake, 3))
        self.assertEqual(watch.uncredited, [])

    def test_a_defeat_that_the_table_does_not_explain_is_uncredited(self):
        fake, _, watch = watching()
        write_entities(fake, {OWN: entity_record(3, 5, 10, (0, 0)),
                              OTHER: entity_record(3, 6, 10, (0, 0))})
        strike(fake, 3)
        self.assertEqual(watch.look(), [])
        self.assertEqual(watch.uncredited, [3])
        self.assertFalse(defeated_bit(fake, 3))
        self.assertEqual(watch.look(), [])
        self.assertEqual(watch.uncredited, [3])

    def test_a_new_defeat_for_a_credited_placement_is_explained_without_an_event(self):
        fake, _, watch = watching()
        write_entities(fake, {OWN: entity_record(3, 5, -1, (0, 0)),
                              OTHER: entity_record(3, 6, 10, (0, 0))})
        strike(fake, 3)
        watch.look()
        # A part that records MERMAN_A's entity as its parent is defeated next.
        write_entities(fake, {OWN: entity_record(3, 5, 5, (0, 0)),
                              0x60: entity_record(3, 0, -1, (0, 0), link=game.ENTITY_BASE
                                                  + OWN * game.ENTITY_SIZE)})
        strike(fake, 3)
        self.assertEqual(watch.look(), [])
        self.assertEqual(watch.uncredited, [])
        self.assertFalse(defeated_bit(fake, 3))
        self.assertEqual(watch.events, [Event("enemy", "101", "defeated")])

    def test_a_bit_for_a_type_the_watch_does_not_look_for_is_left_alone(self):
        fake, recording, watch = watching(ARENA)
        strike(fake, SKULL.type_id)
        self.assertEqual(watch.look(), [])
        self.assertTrue(defeated_bit(fake, SKULL.type_id))
        self.assertEqual(writes(recording), [])


class TestWatchOnTheRom(unittest.TestCase):
    """Placements that share one spot, told apart by the entries that spawned their entities."""

    @classmethod
    def setUpClass(cls):
        cls.rom = read_rom()

    def test_a_medusa_head_counts_for_each_generator_that_is_present_in_room_615(self):
        target = success.room("615", self.rom)
        fake, _, watch = watching(target, soma=(100, 250))
        write_entities(fake, {OWN: entity_record(57, 7, 1, (0, 0)),
                              OTHER: entity_record(57, 8, 1, (0, 0)),
                              0x60: entity_record(57, 0, -1, (40, 40))})
        strike(fake, 57)
        self.assertEqual(watch.look(), [Event("enemy", "449", "defeated"),
                                        Event("enemy", "450", "defeated")])
        self.assertTrue(defeated_bit(fake, 57))

    def test_only_the_medusa_head_generator_that_is_present_is_credited(self):
        target = success.room("615", self.rom)
        fake, _, watch = watching(target, soma=(100, 250))
        write_entities(fake, {OWN: entity_record(57, 7, 1, (0, 0)),
                              0x60: entity_record(57, 0, -1, (40, 40))})
        strike(fake, 57)
        self.assertEqual(watch.look(), [Event("enemy", "449", "defeated")])
        self.assertFalse(defeated_bit(fake, 57))

    def test_the_two_mermen_at_one_spot_in_room_72d_are_told_apart(self):
        target = success.room("72D", self.rom)
        fake, _, watch = watching(target, soma=(40, 420))
        write_entities(fake, {OWN: entity_record(3, 11, 20, (192, 648)),
                              OTHER: entity_record(3, 12, -3, (192, 648))})
        strike(fake, 3)
        self.assertEqual(watch.look(), [Event("enemy", "639", "defeated")])
        self.assertFalse(defeated_bit(fake, 3))

    def test_leaving_room_500_by_either_door_to_501_names_that_door(self):
        target = success.room("500", self.rom)
        new_room = room_info.by_identifier["501"].room_address
        for soma, door in (((100, 460), "500:501 (left)"), ((400, 460), "500:501 (right)")):
            with self.subTest(door=door):
                fake, _, watch = watching(target, soma=soma)
                watch.look()
                in_play(fake, new_room, (soma[0], 60))
                self.assertEqual(watch.look(), [Event("door", door, "left")])


if __name__ == "__main__":
    unittest.main()
