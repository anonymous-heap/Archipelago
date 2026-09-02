"""
Layout pins for the bytemaker ``Struct`` records (``rom/entity.py``, ``ram/structures.py``).

The offsets here are counted by hand from the ROM/RAM documentation on purpose: the
production code derives them from the declarations (``offset_of`` / ``sizeof``), so a
drift in a declaration surfaces as a failure here instead of as a mis-patched ROM or a
mis-read RAM block.
"""
import unittest

from .._bytemaker_compat import offset_of, sizeof
from ..ram.structures import EQUIPPED_GEAR_SIZE, VITALS_SIZE, EquippedGear, PlayerVitals
from ..rom.entity import ENTITY_SIZE, AoSPickupEntity


class EntityRecordTest(unittest.TestCase):
    EXPECTED_OFFSETS = {"x": 0x00, "y": 0x02, "entity_id": 0x04, "type": 0x05, "subtype": 0x06,
                        "unknown": 0x07, "var_a": 0x08, "var_b": 0x0A}

    def test_is_twelve_bytes(self):
        self.assertEqual(sizeof(AoSPickupEntity), 12)
        self.assertEqual(ENTITY_SIZE, 12)

    def test_field_offsets_match_the_rom_record(self):
        for name, off in self.EXPECTED_OFFSETS.items():
            with self.subTest(field=name):
                self.assertEqual(offset_of(AoSPickupEntity, name), off)

    def test_round_trip_is_byte_exact(self):
        raw = bytes.fromhex("f0ff0180aabbccdd3412cdab")
        e = AoSPickupEntity.parse(raw)
        self.assertEqual((e.x, e.y, e.entity_id, e.type, e.subtype, e.unknown, e.var_a, e.var_b),
                         (-16, -32767, 0xAA, 0xBB, 0xCC, 0xDD, 0x1234, 0xABCD))
        self.assertEqual(e.pack(), raw)

    def test_positions_are_signed(self):
        e = AoSPickupEntity(x=-1, y=-2, entity_id=0, type=0, subtype=0, unknown=0, var_a=0, var_b=0)
        self.assertEqual(e.pack()[:4], b"\xff\xff\xfe\xff")
        self.assertEqual(AoSPickupEntity.parse(e.pack()).x, -1)

    def test_fields_are_plain_ints(self):
        e = AoSPickupEntity.parse(bytes(12))
        for name in self.EXPECTED_OFFSETS:
            with self.subTest(field=name):
                self.assertIs(type(getattr(e, name)), int)

    def test_stores_narrow_to_the_field_width(self):
        # C-bitfield semantics: an oversized store keeps the low bits. Documented so a caller
        # relying on a range *check* knows to write one (see soul_drop_rates.scaled_rate).
        e = AoSPickupEntity.parse(bytes(12))
        e.var_b = 0x1_ABCD
        self.assertEqual(e.var_b, 0xABCD)


class RamRecordTest(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual((sizeof(PlayerVitals), VITALS_SIZE), (8, 8))
        self.assertEqual((sizeof(EquippedGear), EQUIPPED_GEAR_SIZE), (6, 6))

    def test_vitals_layout(self):
        for name, off in {"current_hp": 0, "current_mp": 2, "max_hp": 4, "max_mp": 6}.items():
            with self.subTest(field=name):
                self.assertEqual(offset_of(PlayerVitals, name), off)

    def test_vitals_current_values_are_signed(self):
        # the engine stores and tests HP signed; an overkill hit reads as a negative
        v = PlayerVitals.parse(bytes.fromhex("f6ff" "0000" "e803" "6400"))
        self.assertEqual((v.current_hp, v.current_mp, v.max_hp, v.max_mp), (-10, 0, 1000, 100))
        self.assertEqual(v.pack(), bytes.fromhex("f6ff0000e8036400"))

    def test_gear_layout_and_round_trip(self):
        fields = ("weapon", "red_soul", "blue_soul", "yellow_soul", "armor", "accessory")
        for i, name in enumerate(fields):
            with self.subTest(field=name):
                self.assertEqual(offset_of(EquippedGear, name), i)
        g = EquippedGear.parse(bytes([1, 2, 3, 4, 5, 6]))
        self.assertEqual(tuple(getattr(g, f) for f in fields), (1, 2, 3, 4, 5, 6))
        self.assertEqual(g.pack(), bytes([1, 2, 3, 4, 5, 6]))
