"""
Pins for the EWRAM map (``ram/addresses.py``) and the entry-driven accessors (``ram/accessors.py``).

The offsets below are the EWRAM-domain numbers the map used to carry as hand-kept constants,
written out here on purpose: the production code now derives them from the declarations, so a
drift in a declaration fails here instead of as a mis-read byte on a live game.

The behaviour tests drive ``AoSRAM`` over a fake backend with dict-of-bytearray memory, so
they need no emulator; the BizHawk backend itself is checked against a recording stand-in for
the connector module.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from .. import ram as ram_pkg
from ..ram import AoSRAM, BizHawkBackend, SoulPair, addresses as addr
from ..ram import backend as backend_module
from ..ram.accessors import EWRAM

# entry name -> (EWRAM-domain offset, byte size), as documented against the USA ROM
DOCUMENTED = {
    "GAME_STATE": (0x00010, 1), "MENU_STATE": (0x00064, 1), "GAME_MODE": (0x000A1, 1),
    "GAME_CLEARED_FLAGS": (0x00060, 1), "PICKUP_FLAGS": (0x00360, 0x14), "BOSS_FLAGS": (0x0037E, 2),
    "GLOBAL_FLAGS": (0x0042C, 4), "CURRENT_AREA": (0x0009E, 1), "CURRENT_ROOM": (0x0009F, 1),
    "CURRENT_SAVE_SLOT": (0x00428, 1), "GEAR": (0x13268, 6), "VITALS": (0x1327A, 8),
    "EQUIPPED_WEAPON": (0x13268, 1), "EQUIPPED_RED_SOUL": (0x13269, 1), "EQUIPPED_BLUE_SOUL": (0x1326A, 1),
    "EQUIPPED_YELLOW_SOUL": (0x1326B, 1), "EQUIPPED_ARMOR": (0x1326C, 1), "EQUIPPED_ACCESSORY": (0x1326D, 1),
    "CURRENT_HP": (0x1327A, 2), "CURRENT_MP": (0x1327C, 2), "MAX_HP": (0x1327E, 2), "MAX_MP": (0x13280, 2),
    "CAMERA_X": (0x0A09A, 2), "CAMERA_Y": (0x0A09E, 2),
    "PLAYER_SCREEN_X": (0x00526, 2), "PLAYER_SCREEN_Y": (0x0052A, 2),
    "CURRENT_GOLD": (0x13290, 4), "AP_RECEIVED_COUNT": (0x1328A, 2), "KILL_REQUEST": (0x1324C, 1),
    "ABILITY_ENABLED": (0x13396, 2),
    "EVENT_FLAGS": (0x0033C, 4), "EXPLORED_MAP": (0x000B4, 0x280),
    "CASTLE_MAPS": (0x132B1, 3),  # consumables 29 to 31
}

# category -> (EWRAM-domain base, item count, nibble-packed)
DOCUMENTED_INVENTORY = {
    "consumable": (0x13294, 0x20, False), "weapon": (0x132B4, 0x3B, False), "armor": (0x132EF, 0x19, False),
    "accessory": (0x13308, 0x14, False), "red_soul": (0x1331C, 56, True), "blue_soul": (0x13354, 26, True),
    "yellow_soul": (0x1336E, 36, True), "ability_soul": (0x13392, 6, True),
}


class EwramMapTest(unittest.TestCase):
    def test_plane_geometry(self):
        self.assertEqual((addr.ewram.base, len(addr.ewram)), (0x02000000, 0x40000))
        self.assertEqual(addr.EWRAM, "EWRAM")

    def test_every_entry_requests_its_documented_offset_and_size(self):
        for name, (offset, size) in DOCUMENTED.items():
            with self.subTest(entry=name):
                self.assertEqual(tuple(getattr(addr, name).request()), (offset, size))

    def test_inventory_tables(self):
        for name, (base, length, nibble) in DOCUMENTED_INVENTORY.items():
            with self.subTest(category=name):
                table = addr.INVENTORY[name]
                offset, size = table.entry.request()
                self.assertEqual((offset, table.length, table.nibble_packed), (base, length, nibble))
                self.assertEqual(size, (length + 1) // 2 if nibble else length)
                # the slot holding item 3: byte 3 of a byte array, or pair 1 (souls 2 and 3) of a soul array
                slot = table.entry.item(3 // 2 if nibble else 3)
                self.assertEqual(slot.request().offset, base + (3 // 2 if nibble else 3))

    def test_soul_pair_packing(self):
        pair = SoulPair.parse(b"\x25")
        self.assertEqual((pair.even, pair.odd), (5, 2))   # low nibble = even index
        pair.odd = 8
        self.assertEqual(pair.pack(), b"\x85")


class FakeBackend:
    """A ``RamBackend`` over dict-of-bytearray memory.

    Reads are batched and writes are plain. A guarded write or batch checks every guard before
    applying anything, which matches the connector's frame-atomic behaviour.

    ``read_batches`` and ``write_batches`` record the requests of each ``read_many`` and
    ``guarded_write_many`` call, in order, so a test can check what went into one round-trip.
    """

    def __init__(self, memory: dict) -> None:
        self.memory = memory
        self.read_batches: list[list] = []
        self.write_batches: list[list] = []

    async def read_many(self, requests):
        self.read_batches.append(list(requests))
        return [bytes(self.memory[dom][off:off + size]) for off, size, dom in requests]

    async def write(self, offset, data, domain):
        data = bytes(data)
        self.memory[domain][offset:offset + len(data)] = data

    async def guarded_write(self, offset, data, expected, domain):
        expected = bytes(expected)
        if bytes(self.memory[domain][offset:offset + len(expected)]) != expected:
            return False
        await self.write(offset, data, domain)
        return True

    async def guarded_write_many(self, writes):
        self.write_batches.append(list(writes))
        for w in writes:
            current = bytes(self.memory[w.domain][w.offset:w.offset + len(w.expected)])
            if current != bytes(w.expected):
                return False
        for w in writes:
            await self.write(w.offset, w.data, w.domain)
        return True


class AccessorBehaviourTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mem = bytearray(0x40000)
        self.mem[0x10] = 0x04                                    # INGAME
        self.mem[0x64] = 0x01                                    # NORMAL
        self.mem[0x13268:0x1326E] = bytes([1, 2, 3, 4, 5, 6])    # gear
        self.mem[0x1327A:0x1327C] = (-5 & 0xFFFF).to_bytes(2, "little")
        self.mem[0x1327E:0x13280] = (500).to_bytes(2, "little")
        self.mem[0x1328A:0x1328C] = (5).to_bytes(2, "little")
        self.mem[0x13290:0x13294] = (1000).to_bytes(4, "little")
        self.mem[0x1331C + 2] = 0x25                             # red_soul[4]=5, [5]=2
        self.mem[0x1336E] = 0x93                                 # yellow_soul[0]=3, [1]=9 (cap)
        self.ram = AoSRAM(FakeBackend({EWRAM: self.mem}))

    def run_(self, coro):
        return asyncio.run(coro)

    def test_a_souls_count_includes_its_second_list(self):
        # The file-select Copy moves a file's souls into the second list, and the game's total for
        # a soul sums both lists.
        self.mem[0x13361] = 0x01                                 # blue_soul[0] second list = 1
        self.assertEqual(self.run_(self.ram.owned_count("blue_soul", 0)), 1)
        self.mem[0x13354] = 0x02                                 # first list = 2
        self.assertEqual(self.run_(self.ram.owned_count("blue_soul", 0)), 3)

    def test_the_soul_cap_applies_to_both_lists_together(self):
        self.mem[0x13354] = 0x04                                 # blue_soul[0]: 4 + 5 = 9
        self.mem[0x13361] = 0x05
        with self.assertLogs("Client", level="WARNING"):
            self.assertTrue(self.run_(self.ram.give_item("blue_soul", 0)))
        self.assertEqual(self.mem[0x13354], 0x04, "a soul at the cap must not be added")
        self.mem[0x13361] = 0x03                                 # 4 + 3 = 7: room for one
        self.assertTrue(self.run_(self.ram.give_item("blue_soul", 0)))
        self.assertEqual(self.mem[0x13354], 0x05, "the soul goes into the first list")

    def test_the_first_ability_soul_arrives_switched_on(self):
        self.mem[0x13396] = 0b0000_0001                          # Grave Keeper (0) already on
        self.assertTrue(self.run_(self.ram.give_item("ability_soul", 3)))
        self.assertEqual(self.mem[0x13393], 0x10, "ability_soul[3] owned once")
        self.assertEqual(self.mem[0x13396], 0b0000_1001, "and switched on, keeping Grave Keeper on")

    def test_the_callers_writes_come_last_in_the_batch(self):
        # With the counter's write last, a backend that stops partway through the batch leaves the
        # counter unadvanced, so the next tick grants the item again.
        from ..ram.backend import GuardedWrite
        sentinel = GuardedWrite(0x1328A, b"\x06\x00", b"\x05\x00", EWRAM)
        self.assertTrue(self.run_(self.ram.give_item("ability_soul", 2, together_with=[sentinel])))
        batch = self.ram.backend.write_batches[0]
        self.assertEqual(batch[-1], sentinel)
        self.assertEqual(len(batch), 3, "the soul, its switch, then the caller's write")

    def test_another_copy_keeps_the_players_switch(self):
        self.mem[0x13392] = 0x01                                 # ability_soul[0] owned, switched off
        self.assertTrue(self.run_(self.ram.give_item("ability_soul", 0)))
        self.assertEqual(self.mem[0x13392], 0x02)
        self.assertEqual(self.mem[0x13396], 0, "a copy past the first must leave the switch alone")

    def test_gold_is_added_up_to_the_games_cap(self):
        self.assertTrue(self.run_(self.ram.add_gold(500)))
        self.assertEqual(int.from_bytes(self.mem[0x13290:0x13294], "little"), 1500)
        self.mem[0x13290:0x13294] = (999_000).to_bytes(4, "little")
        self.assertTrue(self.run_(self.ram.add_gold(2000)))
        self.assertEqual(int.from_bytes(self.mem[0x13290:0x13294], "little"), 999_999)

    def souls_collected(self) -> int:
        return int.from_bytes(self.mem[0x13264:0x13266], "little")

    def test_run_state_is_one_batched_read(self):
        self.assertEqual(self.run_(self.ram.get_run_state()), (4, 1))
        self.assertEqual(self.ram.backend.read_batches, [[(0x10, 1, EWRAM), (0x64, 1, EWRAM)]],
                         "both values must come from one frame")

    def test_deathlink_state_is_one_batched_read(self):
        self.mem[0x1324C] = 1
        self.assertEqual(self.run_(self.ram.get_deathlink_state()), (4, 1, -5, True))
        self.assertEqual(len(self.ram.backend.read_batches), 1,
                         "the four values must come from one frame")

    def test_clearing_the_kill_request_zeroes_the_flag(self):
        self.run_(self.ram.request_kill())
        self.run_(self.ram.clear_kill_request())
        self.assertEqual(self.mem[0x1324C], 0)

    def test_max_hp_reads_its_field_in_the_vitals_block(self):
        self.assertEqual(self.run_(self.ram.get_max_hp()), 500)

    def test_nibble_give_bumps_one_soul_and_keeps_its_neighbour(self):
        self.assertTrue(self.run_(self.ram.give_item("red_soul", 4)))
        self.assertEqual(self.mem[0x1331C + 2], 0x26)
        self.assertTrue(self.run_(self.ram.give_item("red_soul", 5)))
        self.assertEqual(self.mem[0x1331C + 2], 0x36)

    def test_a_granted_soul_counts_toward_the_souls_collected_statistic(self):
        # Vanilla's collect path calls sub_08032DBC(1) after adding the soul; without this the
        # file screen would undercount every soul AP delivers.
        self.assertEqual(self.souls_collected(), 0)
        self.run_(self.ram.give_item("red_soul", 4))
        self.assertEqual(self.souls_collected(), 1)
        self.run_(self.ram.give_item("blue_soul", 3))
        self.assertEqual(self.souls_collected(), 2)

    def test_a_soul_at_the_cap_is_delivered_and_counted_but_not_added(self):
        # yellow_soul[1] is at 9, so its owned-count stays put and the give still reports the soul
        # as delivered. The game counts souls absorbed, not distinct souls owned, so the
        # statistic goes up even though the owned-count cannot.
        with self.assertLogs("Client", level="WARNING"):
            self.assertTrue(self.run_(self.ram.give_item("yellow_soul", 1)))
        self.assertEqual(self.mem[0x1336E], 0x93)
        self.assertEqual(self.souls_collected(), 1)

    def test_a_granted_non_soul_leaves_the_soul_statistic_alone(self):
        self.run_(self.ram.give_item("consumable", 3))
        self.run_(self.ram.give_item("weapon", 3))
        self.assertEqual(self.souls_collected(), 0)

    def test_the_souls_collected_statistic_clamps_where_the_game_clamps(self):
        self.mem[0x13264:0x13266] = (999).to_bytes(2, "little")
        self.run_(self.ram.give_item("red_soul", 4))
        self.assertEqual(self.souls_collected(), 999)

    def test_owned_count_unpacks_the_nibble_that_matches_give(self):
        # 0x25 in red_soul's third byte holds index 4 in the LOW nibble and index 5 in the high
        # one, the same halves give_item bumps. announce derives a soul's isNew from this count,
        # so a swapped mapping would announce every new soul as a duplicate.
        self.assertEqual(self.run_(self.ram.owned_count("red_soul", 4)), 5)
        self.assertEqual(self.run_(self.ram.owned_count("red_soul", 5)), 2)
        self.assertEqual(self.run_(self.ram.owned_count("red_soul", 0)), 0)
        self.run_(self.ram.give_item("red_soul", 5))
        self.assertEqual(self.run_(self.ram.owned_count("red_soul", 5)), 3)

    def test_owned_count_reads_a_plain_byte_array_whole(self):
        self.mem[0x13294 + 7] = 4
        self.assertEqual(self.run_(self.ram.owned_count("consumable", 7)), 4)

    def test_an_index_out_of_range_is_refused_before_any_read(self):
        with self.assertRaises(ValueError):
            self.run_(self.ram.owned_count("red_soul", 56))
        with self.assertRaises(ValueError):
            self.run_(self.ram.give_item("red_soul", 56))
        self.assertEqual(self.ram.backend.read_batches, [])

    def test_received_counter_is_compare_and_swapped(self):
        self.assertTrue(self.run_(self.ram.set_received_count(6, expected=5)))
        self.assertFalse(self.run_(self.ram.set_received_count(9, expected=5)))
        self.assertEqual(self.run_(self.ram.get_received_count()), 6)


class BizHawkBackendTest(unittest.TestCase):
    """The connector shim shapes each primitive into the call ``worlds._bizhawk`` expects."""

    class Connector:
        def __init__(self) -> None:
            self.calls = []

        async def read(self, ctx, reqs):
            self.calls.append(("read", ctx, reqs))
            return [b"\x01" * size for _off, size, _dom in reqs]

        async def write(self, ctx, writes):
            self.calls.append(("write", ctx, writes))

        async def guarded_write(self, ctx, writes, guards):
            self.calls.append(("guarded_write", ctx, writes, guards))
            return True

    def test_primitives_map_onto_the_connector_calls(self):
        connector, ctx = self.Connector(), object()
        with patch.object(backend_module, "bizhawk", connector):
            backend = BizHawkBackend(ctx)
            self.assertEqual(asyncio.run(backend.read_many([(0x10, 1, EWRAM), (0x64, 2, EWRAM)])),
                             [b"\x01", b"\x01\x01"])
            asyncio.run(backend.write(0x10, b"\x04", EWRAM))
            self.assertTrue(asyncio.run(backend.guarded_write(0x10, b"\x05", b"\x04", EWRAM)))
        self.assertEqual(connector.calls, [
            ("read", ctx, [(0x10, 1, EWRAM), (0x64, 2, EWRAM)]),
            ("write", ctx, [(0x10, [4], EWRAM)]),
            ("guarded_write", ctx, [(0x10, [5], EWRAM)], [(0x10, [4], EWRAM)]),
        ])

    def test_a_batch_is_one_connector_call_guarding_every_write(self):
        connector, ctx = self.Connector(), object()
        writes = [backend_module.GuardedWrite(0x10, b"\x05", b"\x04", EWRAM),
                  backend_module.GuardedWrite(0x20, b"\x07\x00", b"\x06\x00", EWRAM)]
        with patch.object(backend_module, "bizhawk", connector):
            self.assertTrue(asyncio.run(BizHawkBackend(ctx).guarded_write_many(writes)))
        self.assertEqual(connector.calls, [
            ("guarded_write", ctx, [(0x10, [5], EWRAM), (0x20, [7, 0], EWRAM)],
             [(0x10, [4], EWRAM), (0x20, [6, 0], EWRAM)]),
        ])

    def test_the_backend_protocol_is_exported_from_the_package(self):
        self.assertIs(ram_pkg.RamBackend, backend_module.RamBackend)
