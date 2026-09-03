"""
Pins for the EWRAM map (``ram/addresses.py``) and the entry-driven accessors (``ram/accessors.py``).

The offsets below are the EWRAM-domain numbers the map used to carry as hand-kept constants,
written out here on purpose: the production code now derives them from the declarations, so a
drift in a declaration fails here instead of as a mis-read byte on a live game.

The behaviour tests drive ``AoSRAM`` over a fake BizHawk with dict-of-bytearray memory, so
they need no emulator.
"""
from __future__ import annotations

import asyncio
import unittest

from .. import ram as ram_pkg
from ..ram import AoSRAM, SoulPair, addresses as addr
from ..ram.accessors import EWRAM
import worlds.cvaos.ram.accessors as accessors_module

# entry name -> (EWRAM-domain offset, byte size), as documented against the USA ROM
DOCUMENTED = {
    "GAME_STATE": (0x00010, 1), "MENU_STATE": (0x00064, 1), "GAME_MODE": (0x000A1, 1),
    "GAME_CLEARED_FLAGS": (0x00060, 1), "PICKUP_FLAGS": (0x00360, 0x14), "BOSS_FLAGS": (0x0037E, 2),
    "GLOBAL_FLAGS": (0x0042C, 4), "CURRENT_AREA": (0x0009E, 1), "CURRENT_ROOM": (0x0009F, 1),
    "CURRENT_SAVE_SLOT": (0x00428, 1), "GEAR": (0x13268, 6), "VITALS": (0x1327A, 8),
    "EQUIPPED_WEAPON": (0x13268, 1), "EQUIPPED_RED_SOUL": (0x13269, 1), "EQUIPPED_BLUE_SOUL": (0x1326A, 1),
    "EQUIPPED_YELLOW_SOUL": (0x1326B, 1), "EQUIPPED_ARMOR": (0x1326C, 1), "EQUIPPED_ACCESSORY": (0x1326D, 1),
    "CURRENT_HP": (0x1327A, 2), "CURRENT_MP": (0x1327C, 2), "MAX_HP": (0x1327E, 2), "MAX_MP": (0x13280, 2),
    "CURRENT_GOLD": (0x13290, 4), "AP_RECEIVED_COUNT": (0x1328A, 2), "KILL_REQUEST": (0x1324C, 1),
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

    def test_pickup_flags_length_is_derived(self):
        self.assertEqual(addr.PICKUP_FLAGS_LEN, 0x14)

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
        self.assertIs(ram_pkg.SoulPair, SoulPair)


class FakeBizHawk:
    """The three worlds._bizhawk calls AoSRAM makes, over dict-of-bytearray memory."""

    def __init__(self, memory: dict) -> None:
        self.memory = memory

    async def read(self, ctx, reqs):
        return [bytes(self.memory[dom][off:off + size]) for off, size, dom in reqs]

    async def write(self, ctx, writes):
        for off, data, dom in writes:
            data = bytes(data)
            self.memory[dom][off:off + len(data)] = data

    async def guarded_write(self, ctx, writes, guards):
        for off, expected, dom in guards:
            expected = bytes(expected)
            if bytes(self.memory[dom][off:off + len(expected)]) != expected:
                return False
        await self.write(ctx, writes)
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
        self._saved = accessors_module.bizhawk
        accessors_module.bizhawk = FakeBizHawk({EWRAM: self.mem})
        self.ram = AoSRAM(None)

    def tearDown(self) -> None:
        accessors_module.bizhawk = self._saved

    def run_(self, coro):
        return asyncio.run(coro)

    def test_run_state_is_one_batched_read(self):
        self.assertEqual(self.run_(self.ram.get_run_state()), (4, 1))
        self.assertTrue(self.run_(self.ram.is_in_gameplay()))

    def test_current_hp_is_signed_and_vitals_decode(self):
        self.assertEqual(self.run_(self.ram.get_current_hp()), -5)
        v = self.run_(self.ram.get_vitals())
        self.assertEqual((v.current_hp, v.max_hp), (-5, 500))

    def test_nibble_give_bumps_one_soul_and_keeps_its_neighbour(self):
        self.assertTrue(self.run_(self.ram.give_item("red_soul", 4)))
        self.assertEqual(self.mem[0x1331C + 2], 0x26)
        self.assertTrue(self.run_(self.ram.give_item("red_soul", 5)))
        self.assertEqual(self.mem[0x1331C + 2], 0x36)

    def test_at_cap_give_is_reported_as_delivered_and_writes_nothing(self):
        with self.assertLogs("Client", level="WARNING"):
            self.assertTrue(self.run_(self.ram.give_item("yellow_soul", 1)))
        self.assertEqual(self.mem[0x1336E], 0x93)

    def test_received_counter_is_compare_and_swapped(self):
        self.assertTrue(self.run_(self.ram.set_received_count(6, expected=5)))
        self.assertFalse(self.run_(self.ram.set_received_count(9, expected=5)))
        self.assertEqual(self.run_(self.ram.get_received_count()), 6)

    def test_gold_add_guards_on_the_whole_word(self):
        self.assertTrue(self.run_(self.ram.add_gold(500)))
        self.assertEqual(self.mem[0x13290:0x13294], (1500).to_bytes(4, "little"))

    def test_kill_player_zeroes_the_signed_hp_field(self):
        self.run_(self.ram.kill_player())
        self.assertEqual(self.mem[0x1327A:0x1327C], b"\x00\x00")
        self.assertEqual(self.run_(self.ram.get_current_hp()), 0)

    def test_index_out_of_range_is_refused_before_any_read(self):
        with self.assertRaises(ValueError):
            self.run_(self.ram.give_item("red_soul", 56))
