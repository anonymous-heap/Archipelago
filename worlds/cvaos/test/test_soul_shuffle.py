"""Structural tests for the enemy soul-drop shuffle (rom/soul_shuffle.py).

These do not run the ROM. They check the committed vanilla table's shape, that a planned shuffle
is always a soul-preserving permutation that leaves progression souls alone, and that the emitted
writes say what the plan says -- including the guaranteed-drop carve-out that stops a one-time
boss from stranding a soul. Confirming the table's field meanings in-game still needs an emulator
playtest -- see rom/soul_shuffle.py.
"""
from __future__ import annotations

import unittest
from random import Random

import worlds.cvaos.rom.soul_shuffle as ss


def _fake_rom() -> bytes:
    """A synthetic base ROM carrying the vanilla soul table and the intro grant site.

    Both regions matter: build_writes verifies the enemy table AND, when asked to move the
    starting soul, the intro's grant/equip instructions.
    """
    size = ss._entry(max(ss.VANILLA)) + ss.ENEMY_STRIDE
    rom = bytearray(size)
    for enemy_id, (soul_type, soul_index, rate) in ss.VANILLA.items():
        off = ss._entry(enemy_id)
        rom[off + ss.SOUL_TYPE_OFF] = soul_type
        rom[off + ss.SOUL_INDEX_OFF] = soul_index
        rom[off + ss.SOUL_RATE_OFF] = rate
    for offset, vanilla in ss.STARTING_VANILLA_BYTES.items():
        rom[offset:offset + len(vanilla)] = vanilla
    return bytes(rom)


ROM = _fake_rom()
ALL_MODES = (ss.MODE_WITHIN_TYPE, ss.MODE_ANY_TYPE)


class TestVanillaTable(unittest.TestCase):
    def test_table_covers_every_enemy(self):
        self.assertEqual(sorted(ss.VANILLA), list(range(113)))

    def test_soul_dropping_counts(self):
        with_soul = [e for e, (_t, index, _r) in ss.VANILLA.items() if index]
        self.assertEqual(len(with_soul), 110)
        self.assertEqual(len(ss.VANILLA) - len(with_soul), 3)

    def test_mapping_is_one_to_one(self):
        """No soul has two enemy sources -- this is what makes a permutation lossless."""
        pairs = [(t, index) for t, index, _r in ss.VANILLA.values() if index]
        self.assertEqual(len(pairs), len(set(pairs)))

    def test_soul_types_are_known(self):
        for soul_type, index, _rate in ss.VANILLA.values():
            if index:
                self.assertIn(soul_type, (ss.SOUL_TYPE_RED, ss.SOUL_TYPE_BLUE,
                                          ss.SOUL_TYPE_YELLOW, ss.SOUL_TYPE_ABILITY))

    def test_progression_enemies_all_drop_a_soul(self):
        for enemy_id in ss.PROGRESSION_SOUL_ENEMIES:
            self.assertNotEqual(ss.VANILLA[enemy_id][1], 0)

    def test_shuffle_pool_size(self):
        self.assertEqual(len(ss.shuffleable_enemies(ss.MODE_WITHIN_TYPE)), 103)
        self.assertEqual(ss.shuffleable_enemies(ss.MODE_OFF), [])


class TestPlanShuffle(unittest.TestCase):
    def test_off_plans_nothing(self):
        self.assertEqual(ss.plan_shuffle(Random(1), ss.MODE_OFF), {})

    def test_deterministic_for_a_seed(self):
        for mode in ALL_MODES:
            with self.subTest(mode=mode):
                self.assertEqual(ss.plan_shuffle(Random(7), mode),
                                 ss.plan_shuffle(Random(7), mode))

    def test_plan_is_a_permutation(self):
        for mode in ALL_MODES:
            for seed in range(20):
                with self.subTest(mode=mode, seed=seed):
                    plan = ss.plan_shuffle(Random(seed), mode)
                    self.assertEqual(sorted(plan), sorted(plan.values()))

    def test_never_touches_progression_or_soulless_enemies(self):
        for mode in ALL_MODES:
            for seed in range(20):
                plan = ss.plan_shuffle(Random(seed), mode)
                with self.subTest(mode=mode, seed=seed):
                    self.assertFalse(set(plan) & ss.PROGRESSION_SOUL_ENEMIES)
                    self.assertFalse(set(plan.values()) & ss.PROGRESSION_SOUL_ENEMIES)
                    for enemy_id in plan:
                        self.assertNotEqual(ss.VANILLA[enemy_id][1], 0)

    def test_within_type_preserves_type(self):
        for seed in range(20):
            plan = ss.plan_shuffle(Random(seed), ss.MODE_WITHIN_TYPE)
            with self.subTest(seed=seed):
                for target, source in plan.items():
                    self.assertEqual(ss.VANILLA[target][0], ss.VANILLA[source][0])

    def test_any_type_eventually_crosses_types(self):
        crossed = any(
            ss.VANILLA[t][0] != ss.VANILLA[s][0]
            for seed in range(10)
            for t, s in ss.plan_shuffle(Random(seed), ss.MODE_ANY_TYPE).items())
        self.assertTrue(crossed)

    def test_shuffle_actually_moves_souls(self):
        for mode in ALL_MODES:
            with self.subTest(mode=mode):
                self.assertGreater(len(ss.plan_shuffle(Random(3), mode)), 50)


class TestBuildWrites(unittest.TestCase):
    def test_no_plan_no_writes(self):
        self.assertEqual(ss.build_writes(ROM, {}, True), {})

    def test_writes_the_source_soul(self):
        plan = ss.plan_shuffle(Random(11), ss.MODE_WITHIN_TYPE)
        writes = ss.build_writes(ROM, plan, False)
        for target, source in plan.items():
            soul_type, soul_index, _rate = ss.VANILLA[source]
            off = ss._entry(target)
            self.assertEqual(writes[off + ss.SOUL_TYPE_OFF], bytes([soul_type]))
            self.assertEqual(writes[off + ss.SOUL_INDEX_OFF], bytes([soul_index]))

    def test_enemy_rates_mode_leaves_rates_alone(self):
        plan = ss.plan_shuffle(Random(11), ss.MODE_WITHIN_TYPE)
        writes = ss.build_writes(ROM, plan, keep_soul_drop_rates=False)
        for target in plan:
            self.assertNotIn(ss._entry(target) + ss.SOUL_RATE_OFF, writes)

    def test_soul_rates_mode_moves_the_rate(self):
        plan = ss.plan_shuffle(Random(11), ss.MODE_WITHIN_TYPE)
        writes = ss.build_writes(ROM, plan, keep_soul_drop_rates=True)
        moved = 0
        for target, source in plan.items():
            key = ss._entry(target) + ss.SOUL_RATE_OFF
            if ss.VANILLA[target][2] == 0:
                continue
            self.assertEqual(writes[key], bytes([ss.VANILLA[source][2]]))
            moved += 1
        self.assertGreater(moved, 0)

    def test_guaranteed_drops_stay_guaranteed(self):
        """A vanilla rate of 0 marks an unfarmable kill; it must never inherit a rarer rate."""
        guaranteed = {e for e, (_t, index, rate) in ss.VANILLA.items() if index and rate == 0}
        self.assertTrue(guaranteed)
        for seed in range(30):
            plan = ss.plan_shuffle(Random(seed), ss.MODE_ANY_TYPE)
            writes = ss.build_writes(ROM, plan, keep_soul_drop_rates=True)
            with self.subTest(seed=seed):
                for enemy_id in guaranteed:
                    self.assertNotIn(ss._entry(enemy_id) + ss.SOUL_RATE_OFF, writes)

    def test_every_soul_still_has_exactly_one_source(self):
        """The whole point: applying the writes permutes sources without losing a soul."""
        vanilla_souls = sorted((t, i) for t, i, _r in ss.VANILLA.values() if i)
        for mode in ALL_MODES:
            for seed in range(10):
                plan = ss.plan_shuffle(Random(seed), mode)
                patched = bytearray(ROM)
                for off, data in ss.build_writes(ROM, plan, True).items():
                    patched[off:off + len(data)] = data
                after = sorted(
                    (patched[ss._entry(e) + ss.SOUL_TYPE_OFF],
                     patched[ss._entry(e) + ss.SOUL_INDEX_OFF])
                    for e in ss.VANILLA
                    if patched[ss._entry(e) + ss.SOUL_INDEX_OFF])
                with self.subTest(mode=mode, seed=seed):
                    self.assertEqual(after, vanilla_souls)

    def test_progression_souls_stay_on_their_enemy(self):
        for mode in ALL_MODES:
            for seed in range(10):
                plan = ss.plan_shuffle(Random(seed), mode)
                writes = ss.build_writes(ROM, plan, True)
                with self.subTest(mode=mode, seed=seed):
                    for enemy_id in ss.PROGRESSION_SOUL_ENEMIES:
                        off = ss._entry(enemy_id)
                        for field in (ss.SOUL_TYPE_OFF, ss.SOUL_INDEX_OFF, ss.SOUL_RATE_OFF):
                            self.assertNotIn(off + field, writes)


class TestGuards(unittest.TestCase):
    def test_rom_mismatch_is_rejected(self):
        bad = bytearray(ROM)
        bad[ss._entry(0) + ss.SOUL_INDEX_OFF] ^= 0xFF
        with self.assertRaisesRegex(ValueError, "ROM mismatch"):
            ss.build_writes(bytes(bad), {}, True)

    def test_non_permutation_is_rejected(self):
        eligible = ss.shuffleable_enemies(ss.MODE_ANY_TYPE)
        a, b, c = eligible[0], eligible[1], eligible[2]
        with self.assertRaisesRegex(ValueError, "not a permutation"):
            ss.build_writes(ROM, {a: b, b: c}, True)

    def test_progression_target_is_rejected(self):
        victim = min(ss.PROGRESSION_SOUL_ENEMIES)
        with self.assertRaisesRegex(ValueError, "progression-soul enemy"):
            ss.build_writes(ROM, {victim: victim}, True)

    def test_soulless_target_is_rejected(self):
        soulless = next(e for e, (_t, index, _r) in ss.VANILLA.items() if index == 0)
        with self.assertRaisesRegex(ValueError, "drops no soul"):
            ss.build_writes(ROM, {soulless: soulless}, True)


class TestStartingSoul(unittest.TestCase):
    """The intro's free soul is hardcoded immediates, not an enemy drop."""

    def test_starting_enemy_is_the_winged_skeleton_one(self):
        soul_type, index, _rate = ss.VANILLA[ss.STARTING_SOUL_ENEMY]
        self.assertEqual((soul_type, index), (ss.SOUL_TYPE_RED, 1))   # red, 1-based idx 1 == 0
        self.assertNotIn(ss.STARTING_SOUL_ENEMY, ss.PROGRESSION_SOUL_ENEMIES)

    def test_off_by_default_leaves_the_intro_alone(self):
        plan = ss.plan_shuffle(Random(4), ss.MODE_WITHIN_TYPE)
        writes = ss.build_writes(ROM, plan, True)
        for offset in ss.STARTING_VANILLA_BYTES:
            self.assertNotIn(offset, writes)

    def test_grants_whatever_that_enemy_now_drops(self):
        for mode in ALL_MODES:
            for seed in range(15):
                plan = ss.plan_shuffle(Random(seed), mode)
                source = plan.get(ss.STARTING_SOUL_ENEMY)
                if source is None:
                    continue
                writes = ss.build_writes(ROM, plan, True, shuffle_starting_soul=True)
                soul_type, index, _rate = ss.VANILLA[source]
                with self.subTest(mode=mode, seed=seed):
                    # the grant takes a 0-based index; the table stores 1-based
                    self.assertEqual(writes[ss.STARTING_GRANT_TYPE_OFF], bytes([soul_type]))
                    self.assertEqual(writes[ss.STARTING_GRANT_INDEX_OFF], bytes([index - 1]))

    def test_equip_targets_the_slot_matching_the_soul_type(self):
        """Any soul type is fine: the equip is repointed at its own equipped-soul slot."""
        seen_types = set()
        for mode in ALL_MODES:
            for seed in range(40):
                plan = ss.plan_shuffle(Random(seed), mode)
                source = plan.get(ss.STARTING_SOUL_ENEMY)
                if source is None:
                    continue
                soul_type, index, _rate = ss.VANILLA[source]
                writes = ss.build_writes(ROM, plan, True, shuffle_starting_soul=True)
                displacement = ss.EQUIP_SLOT_DISPLACEMENT[soul_type]
                with self.subTest(mode=mode, seed=seed, soul_type=soul_type):
                    self.assertEqual(writes[ss.STARTING_EQUIP_OFF], bytes([index]))
                    self.assertEqual(writes[ss.STARTING_EQUIP_LOAD_OFF],
                                     ss._ldrb_r0_from_r4(displacement))
                    self.assertEqual(writes[ss.STARTING_EQUIP_STORE_OFF],
                                     ss._strb_r0_to_r4(displacement))
                seen_types.add(soul_type)
        self.assertIn(ss.SOUL_TYPE_RED, seen_types)
        self.assertTrue(seen_types - {ss.SOUL_TYPE_RED},
                        "no non-red starting soul covered (needs any_type)")

    def test_within_type_keeps_the_start_red(self):
        for seed in range(30):
            plan = ss.plan_shuffle(Random(seed), ss.MODE_WITHIN_TYPE)
            source = plan.get(ss.STARTING_SOUL_ENEMY)
            if source is None:
                continue
            with self.subTest(seed=seed):
                self.assertEqual(ss.VANILLA[source][0], ss.SOUL_TYPE_RED)
                writes = ss.build_writes(ROM, plan, True, shuffle_starting_soul=True)
                self.assertEqual(writes[ss.STARTING_EQUIP_LOAD_OFF],
                                 ss.STARTING_VANILLA_BYTES[ss.STARTING_EQUIP_LOAD_OFF])

    def test_equip_instruction_encoding_round_trips(self):
        """The rebuilt ldrb/strb must decode back to the intended displacement."""
        for soul_type, displacement in ss.EQUIP_SLOT_DISPLACEMENT.items():
            with self.subTest(soul_type=soul_type):
                load = int.from_bytes(ss._ldrb_r0_from_r4(displacement), "little")
                store = int.from_bytes(ss._strb_r0_to_r4(displacement), "little")
                for word, opcode in ((load, 0x7800), (store, 0x7000)):
                    self.assertEqual(word & 0xF800, opcode)      # ldrb / strb, byte form
                    self.assertEqual((word >> 6) & 0x1F, displacement)
                    self.assertEqual((word >> 3) & 0x07, 4)      # base register r4
                    self.assertEqual(word & 0x07, 0)             # source/dest r0

    def test_vanilla_equip_bytes_match_a_rebuilt_red_equip(self):
        """Sanity-check the encoder against the bytes actually in the ROM."""
        red = ss.EQUIP_SLOT_DISPLACEMENT[ss.SOUL_TYPE_RED]
        self.assertEqual(ss._ldrb_r0_from_r4(red),
                         ss.STARTING_VANILLA_BYTES[ss.STARTING_EQUIP_LOAD_OFF])
        self.assertEqual(ss._strb_r0_to_r4(red),
                         ss.STARTING_VANILLA_BYTES[ss.STARTING_EQUIP_STORE_OFF])

    def test_no_plan_means_no_starting_write(self):
        self.assertEqual(ss.build_writes(ROM, {}, True, shuffle_starting_soul=True), {})

    def test_rom_mismatch_at_the_grant_site_is_rejected(self):
        bad = bytearray(ROM)
        bad[ss.STARTING_GRANT_INDEX_OFF] = 0x5A
        plan = ss.plan_shuffle(Random(4), ss.MODE_WITHIN_TYPE)
        with self.assertRaisesRegex(ValueError, "starting-soul site"):
            ss.build_writes(bytes(bad), plan, True, shuffle_starting_soul=True)

    def test_patch_widths_match_vanilla(self):
        """Every write must be exactly as wide as the bytes it replaces."""
        for mode in ALL_MODES:
            for seed in range(15):
                plan = ss.plan_shuffle(Random(seed), mode)
                if ss.STARTING_SOUL_ENEMY not in plan:
                    continue
                writes = ss.build_writes(ROM, plan, True, shuffle_starting_soul=True)
                for offset, vanilla in ss.STARTING_VANILLA_BYTES.items():
                    with self.subTest(mode=mode, seed=seed, offset=offset):
                        self.assertEqual(len(writes[offset]), len(vanilla))


class TestWiring(unittest.TestCase):
    """The seams between the option, rom_config.json and build_writes."""

    def test_every_soul_shuffle_option_maps_to_a_mode(self):
        from worlds.cvaos.options import SoulShuffle
        from worlds.cvaos.rom.patch import _SOUL_SHUFFLE_MODES

        option_values = {value for name, value in vars(SoulShuffle).items()
                         if name.startswith("option_")}
        self.assertEqual(set(_SOUL_SHUFFLE_MODES), option_values)
        for mode in _SOUL_SHUFFLE_MODES.values():
            self.assertIn(mode, (ss.MODE_OFF, ss.MODE_WITHIN_TYPE, ss.MODE_ANY_TYPE))

    def test_plan_survives_the_json_round_trip(self):
        """rom_config.json stringifies dict keys; apply_rom_features must undo that."""
        import json

        plan = ss.plan_shuffle(Random(5), ss.MODE_WITHIN_TYPE)
        revived = {int(target): source
                   for target, source in json.loads(json.dumps(plan)).items()}
        self.assertEqual(revived, plan)
        self.assertEqual(ss.build_writes(ROM, revived, True),
                         ss.build_writes(ROM, plan, True))


if __name__ == "__main__":
    unittest.main()
