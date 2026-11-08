"""Structural tests for the enemy soul-drop shuffle (rom/soul_shuffle.py).

These do not run the game. They check the committed vanilla table's shape, that a planned shuffle
is always a soul-preserving permutation that leaves progression souls alone, and that the emitted
writes say what the plan says. When rates move with their souls, a scripted boss still keeps its
rate of 0, and an ordinary enemy that receives a boss's soul keeps its own rate. Under matched, a
soul trades places only within its color and tier, and every rate byte stays on its row.

These tests cannot confirm what the table's fields mean in the game. rom/soul_shuffle.py says
where those meanings come from.
"""
from __future__ import annotations

import unittest
from random import Random

import worlds.cvaos.rom.soul_shuffle as ss
from worlds.cvaos.test import early, run_apply_rom_features


def _fake_rom() -> bytes:
    """Return a stand-in base ROM that holds the vanilla soul table and the intro's grant site.

    build_writes verifies the enemy table, and when it moves the starting soul it also verifies
    the intro's grant and equip instructions.
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

# The four scripted bosses: their death scripts award the soul in their row. Three of them also run
# the generic death routine, and rate 0 keeps it from rolling for that soul a second time.
SCRIPTED_BOSSES = frozenset(e for e, (_t, index, rate) in ss.VANILLA.items() if index and rate == 0)


def _apply(rom: bytes, writes: dict) -> bytearray:
    patched = bytearray(rom)
    for off, data in writes.items():
        patched[off:off + len(data)] = data
    return patched


def _assert_every_soul_obtainable(test: unittest.TestCase, patched: bytes) -> None:
    """Every vanilla soul has exactly one row, and that row can award it: a scripted boss row
    keeps rate 0, and any other row holding a soul has a nonzero rate. Each progression soul is
    also still on its vanilla enemy, where logic expects it."""
    souls = []
    for enemy_id in ss.VANILLA:
        row = ss._entry(enemy_id)
        soul = (patched[row + ss.SOUL_TYPE_OFF], patched[row + ss.SOUL_INDEX_OFF])
        rate = patched[row + ss.SOUL_RATE_OFF]
        if enemy_id in SCRIPTED_BOSSES:
            test.assertNotEqual(soul[1], 0, f"boss {enemy_id} lost its soul")
            test.assertEqual(rate, 0, f"boss {enemy_id} gained a random roll")
        elif soul[1]:
            test.assertNotEqual(rate, 0, f"enemy {enemy_id} can never drop {soul}")
        if enemy_id in ss.PROGRESSION_SOUL_ENEMIES:
            test.assertEqual(soul, ss.VANILLA[enemy_id][:2],
                             f"enemy {enemy_id} lost its progression soul")
        if soul[1]:
            souls.append(soul)
    test.assertCountEqual(souls, [(t, i) for t, i, _r in ss.VANILLA.values() if i])


# The modes that need no groups. TestMatched covers matched.
ALL_MODES = (ss.MODE_WITHIN_TYPE, ss.MODE_ANY_TYPE)


class TestVanillaTable(unittest.TestCase):
    def test_table_covers_every_enemy(self):
        self.assertEqual(sorted(ss.VANILLA), list(range(113)))

    def test_soul_dropping_counts(self):
        with_soul = [e for e, (_t, index, _r) in ss.VANILLA.items() if index]
        self.assertEqual(len(with_soul), 110)
        self.assertEqual(len(ss.VANILLA) - len(with_soul), 3)

    def test_mapping_is_one_to_one(self):
        """No soul has two enemy sources, so permuting the sources loses no soul."""
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
        moved = boss_sources = 0
        for target, source in plan.items():
            key = ss._entry(target) + ss.SOUL_RATE_OFF
            if ss.VANILLA[target][2] == 0:
                continue
            if ss.VANILLA[source][2] == 0:
                self.assertNotIn(key, writes, "a boss's 0 must not move to an ordinary enemy")
                boss_sources += 1
                continue
            self.assertEqual(writes[key], bytes([ss.VANILLA[source][2]]))
            moved += 1
        self.assertGreater(moved, 0)
        self.assertGreater(boss_sources, 0)

    def test_a_boss_soul_on_an_ordinary_enemy_takes_that_enemys_rate(self):
        """An ordinary enemy that receives a boss's soul keeps its own rate, because moving the
        boss's 0 would stop the soul from dropping."""
        legion, bat = 108, 0
        self.assertEqual(ss.VANILLA[legion][2], 0, "premise: Legion's soul is a scripted award")
        patched = _apply(ROM, ss.build_writes(ROM, {bat: legion, legion: bat}, keep_soul_drop_rates=True))
        self.assertEqual(patched[ss._entry(bat) + ss.SOUL_RATE_OFF], ss.VANILLA[bat][2])
        self.assertEqual(patched[ss._entry(bat) + ss.SOUL_INDEX_OFF], ss.VANILLA[legion][1])
        self.assertEqual(patched[ss._entry(legion) + ss.SOUL_RATE_OFF], 0)
        self.assertEqual(patched[ss._entry(legion) + ss.SOUL_INDEX_OFF], ss.VANILLA[bat][1])

    def test_every_soul_can_still_be_obtained(self):
        for mode in ALL_MODES:
            for keep in (True, False):
                for seed in range(10):
                    plan = ss.plan_shuffle(Random(seed), mode)
                    patched = _apply(ROM, ss.build_writes(ROM, plan, keep))
                    with self.subTest(mode=mode, keep=keep, seed=seed):
                        _assert_every_soul_obtainable(self, patched)

    def test_rate_edits_after_the_shuffle_keep_every_soul_obtainable(self):
        """The patch applies the shuffle, then the per-enemy rate overrides, then the multiplier.
        After all three, every soul is still obtainable: no ordinary enemy holding a soul has a rate
        of 0, and no boss has gained a random roll."""
        from worlds.cvaos.rom import soul_drop_rates as sdr
        for mode in ALL_MODES:
            for seed in range(5):
                plan = ss.plan_shuffle(Random(seed), mode)
                patched = _apply(ROM, ss.build_writes(ROM, plan, True))
                patched = _apply(patched, sdr.build_writes(bytes(patched)))
                patched = _apply(patched, sdr.build_multiplier_writes(bytes(patched), 1000))
                with self.subTest(mode=mode, seed=seed):
                    _assert_every_soul_obtainable(self, patched)


class TestMatched(unittest.TestCase):
    """matched trades souls only within a color and tier, and every rate byte stays on its row."""

    @classmethod
    def setUpClass(cls) -> None:
        from worlds.cvaos.rom.patch import matched_groups
        cls.groups = matched_groups()
        cls.plans = [ss.plan_shuffle(Random(seed), ss.MODE_MATCHED, cls.groups)
                     for seed in range(20)]

    def test_souls_trade_places_only_within_their_color_and_tier(self):
        self.assertEqual(sorted(self.groups), ss.shuffleable_enemies(ss.MODE_MATCHED))
        for seed, plan in enumerate(self.plans):
            with self.subTest(seed=seed):
                self.assertGreater(len(plan), 50)
                for target, source in plan.items():
                    self.assertEqual(self.groups[target], self.groups[source])
                _assert_every_soul_obtainable(self, _apply(ROM, ss.build_writes(ROM, plan, False)))

    def test_every_rate_byte_stays_on_its_row_whatever_keep_soul_drop_rates_says(self):
        from worlds.cvaos.rom.patch import build_rom_config
        config = build_rom_config(early(soul_shuffle="matched", keep_soul_drop_rates=True))
        plan = {int(target): source for target, source in config["soul_shuffle_plan"].items()}
        self.assertTrue(plan)
        patched = _apply(ROM, ss.build_writes(ROM, plan, config["keep_soul_drop_rates"],
                                              config["shuffle_starting_soul"]))
        for enemy_id, (_type, _index, rate) in ss.VANILLA.items():
            self.assertEqual(patched[ss._entry(enemy_id) + ss.SOUL_RATE_OFF], rate, enemy_id)

    def test_the_starting_soul_is_a_red_soul_of_winged_skeletons_tier(self):
        from worlds.cvaos.soul_sources import soul_name
        from worlds.cvaos.data.item_balancing.soul_tiers import matched_group
        for seed, plan in enumerate(self.plans):
            patched = _apply(ROM, ss.build_writes(ROM, plan, False, shuffle_starting_soul=True))
            soul_type = patched[ss.STARTING_GRANT_TYPE_OFF]
            granted = soul_name(soul_type, patched[ss.STARTING_GRANT_INDEX_OFF] + 1)
            with self.subTest(seed=seed, soul=granted):
                self.assertEqual(soul_type, ss.SOUL_TYPE_RED)
                self.assertEqual(matched_group(granted), matched_group("Winged Skeleton"))


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
    """The intro grants its free soul through instruction immediates, not as an enemy drop."""

    def test_starting_enemy_is_the_winged_skeleton_one(self):
        soul_type, index, _rate = ss.VANILLA[ss.STARTING_SOUL_ENEMY]
        self.assertEqual((soul_type, index), (ss.SOUL_TYPE_RED, 1))   # red, 1-based index 1
        self.assertNotIn(ss.STARTING_SOUL_ENEMY, ss.PROGRESSION_SOUL_ENEMIES)

    def test_off_by_default_leaves_the_intro_alone(self):
        plan = ss.plan_shuffle(Random(4), ss.MODE_WITHIN_TYPE)
        writes = ss.build_writes(ROM, plan, True)
        for offset in ss.STARTING_VANILLA_BYTES:
            self.assertNotIn(offset, writes)

    def test_grants_and_equips_the_soul_that_enemy_now_drops(self):
        """Swapping the starting enemy's soul with another enemy's retargets all five intro writes.

        The grant takes a 0-based index, while the equip field is 1-based. The equip's load and
        store address the equipped-soul slot that matches the new soul's type.
        """
        # soul type -> the equip's ldrb and strb, r0 to or from [r4, #slot]
        equips = {
            ss.SOUL_TYPE_RED: (bytes.fromhex("607b"), bytes.fromhex("6073")),      # slot 0xd
            ss.SOUL_TYPE_BLUE: (bytes.fromhex("a07b"), bytes.fromhex("a073")),     # slot 0xe
            ss.SOUL_TYPE_YELLOW: (bytes.fromhex("e07b"), bytes.fromhex("e073")),   # slot 0xf
        }
        for soul_type, (load, store) in equips.items():
            other = next(e for e in ss.shuffleable_enemies(ss.MODE_ANY_TYPE)
                         if ss.VANILLA[e][0] == soul_type and e != ss.STARTING_SOUL_ENEMY)
            _type, index, _rate = ss.VANILLA[other]
            plan = {ss.STARTING_SOUL_ENEMY: other, other: ss.STARTING_SOUL_ENEMY}
            writes = ss.build_writes(ROM, plan, True, shuffle_starting_soul=True)
            with self.subTest(soul_type=soul_type, enemy=other):
                self.assertEqual(writes[ss.STARTING_GRANT_TYPE_OFF], bytes([soul_type]))
                self.assertEqual(writes[ss.STARTING_GRANT_INDEX_OFF], bytes([index - 1]))
                self.assertEqual(writes[ss.STARTING_EQUIP_LOAD_OFF], load)
                self.assertEqual(writes[ss.STARTING_EQUIP_OFF], bytes([index]))
                self.assertEqual(writes[ss.STARTING_EQUIP_STORE_OFF], store)

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


class TestWiring(unittest.TestCase):
    """How the option's value reaches build_writes through rom_config.json."""

    def test_every_soul_shuffle_option_maps_to_a_mode(self):
        from worlds.cvaos.options import SoulShuffle
        from worlds.cvaos.rom.patch import _SOUL_SHUFFLE_MODES

        option_values = {value for name, value in vars(SoulShuffle).items()
                         if name.startswith("option_")}
        self.assertEqual(set(_SOUL_SHUFFLE_MODES), option_values)
        for mode in _SOUL_SHUFFLE_MODES.values():
            self.assertIn(mode, (ss.MODE_OFF, ss.MODE_WITHIN_TYPE, ss.MODE_ANY_TYPE,
                                 ss.MODE_MATCHED))

    def test_build_writes_receives_the_plan_with_integer_keys(self):
        """rom_config.json stores the plan's keys as strings, so apply_rom_features converts them
        back to enemy ids before it builds the shuffle."""
        plan = ss.plan_shuffle(Random(5), ss.MODE_WITHIN_TYPE)
        stubs = run_apply_rom_features(self, ROM, soul_shuffle_plan=plan,
                                       keep_soul_drop_rates=True, shuffle_starting_soul=False)
        stubs["soul_shuffle"].assert_called_once()
        self.assertEqual(stubs["soul_shuffle"].call_args.args[1], plan)


if __name__ == "__main__":
    unittest.main()
