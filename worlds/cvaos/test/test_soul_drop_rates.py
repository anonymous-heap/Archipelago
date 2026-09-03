"""Tests for the enemy soul-drop rate edits (rom/soul_drop_rates.py).

The rate byte at entry +0x12 only reaches the drop chance as ``rate*8 + 32``, so the chance
ratio between two rates is exactly ``(old + 4) / (new + 4)``. These tests pin that model down
(including against the module's own documented overrides), then check the global multiplier
inverts it, preserves relative rarity, and never touches a guaranteed drop.

No ROM is run; the synthetic table below stands in for the real one.
"""
from __future__ import annotations

import unittest

import worlds.cvaos.rom.soul_drop_rates as sdr

# A spread of real vanilla rates: guaranteed, common, typical, rare, rarest.
SAMPLE_RATES = (0, 1, 3, 7, 10, 12, 15, 20, 25, 30, 32, 50, 60, 80, 100, 120, 150, 180)


def _fake_rom(rate_for_enemy) -> bytes:
    size = sdr._rate_offset(sdr.ENEMY_COUNT - 1) + 1
    rom = bytearray(size)
    for enemy_id in range(sdr.ENEMY_COUNT):
        rom[sdr._rate_offset(enemy_id)] = rate_for_enemy(enemy_id)
    return bytes(rom)


def _chance(rate: int) -> float:
    """Relative drop chance for a rate byte, LCK and numerator cancelled out."""
    return 1.0 / (rate + sdr.RATE_CHANCE_BIAS)


class TestChanceModel(unittest.TestCase):
    def test_bias_reproduces_the_documented_overrides(self):
        """Each SOUL_RATE_OVERRIDES comment states a multiplier; the model must match it."""
        expected = {54: 4.00, 48: 4.00, 43: 4.15}
        for enemy_id, (name, old, new) in sdr.SOUL_RATE_OVERRIDES.items():
            with self.subTest(enemy=name):
                self.assertAlmostEqual(_chance(new) / _chance(old), expected[enemy_id],
                                       places=2)

    def test_overrides_only_ever_help(self):
        for name, old, new in sdr.SOUL_RATE_OVERRIDES.values():
            with self.subTest(enemy=name):
                self.assertLess(new, old)


class TestScaledRate(unittest.TestCase):
    def test_hundred_percent_is_identity(self):
        for rate in SAMPLE_RATES:
            with self.subTest(rate=rate):
                self.assertEqual(sdr.scaled_rate(rate, 100), rate)

    def test_guaranteed_drops_are_untouched(self):
        for percent in (100, 120, 200, 400, 1000):
            with self.subTest(percent=percent):
                self.assertEqual(sdr.scaled_rate(0, percent), 0)

    def test_picks_the_closest_achievable_multiplier(self):
        """
        Only 256 multipliers are expressible for a given rate -- ``(rate+4)/(n+4)`` for each
        byte value n -- so the contract is "land on the nearest one", not "hit the request
        exactly". This checks the choice against an exhaustive search of every byte.

        It also captures the ceiling implicitly: byte 0 is excluded from the search because the
        death routine skips the roll for it, so for a rate of 1 nothing is more common than the
        rate itself and every request resolves to 1.
        """
        for percent in (120, 150, 200, 400, 1000):
            for rate in SAMPLE_RATES:
                if rate == 0:
                    continue
                wanted = percent / 100
                chosen = sdr.scaled_rate(rate, percent)
                best = min(range(1, 0x100),
                           key=lambda n: (abs(_chance(n) / _chance(rate) - wanted), n))
                with self.subTest(percent=percent, rate=rate):
                    self.assertAlmostEqual(
                        abs(_chance(chosen) / _chance(rate) - wanted),
                        abs(_chance(best) / _chance(rate) - wanted),
                        places=9)

    def test_ceiling_is_reached_when_the_request_exceeds_it(self):
        """Floors at 1 once the request passes what the rate can express (0 would be no drop)."""
        for percent in (120, 200, 400, 1000):
            for rate in SAMPLE_RATES:
                if rate == 0:
                    continue
                ceiling = (rate + sdr.RATE_CHANCE_BIAS) / (1 + sdr.RATE_CHANCE_BIAS)
                with self.subTest(percent=percent, rate=rate):
                    if percent / 100 > ceiling:
                        self.assertEqual(sdr.scaled_rate(rate, percent), 1)

    def test_default_multiplier_keeps_the_rare_tail_farmable_not_guaranteed(self):
        """At 1.2x the souls that matter still need farming, and no soul is ever switched off."""
        for rate in SAMPLE_RATES:
            if rate == 0:
                continue
            with self.subTest(rate=rate):
                self.assertGreater(sdr.scaled_rate(rate, 120), 0)

    def test_never_makes_a_soul_rarer(self):
        for percent in (120, 150, 200, 400, 1000):
            for rate in SAMPLE_RATES:
                with self.subTest(percent=percent, rate=rate):
                    self.assertLessEqual(sdr.scaled_rate(rate, percent), rate)

    def test_stays_a_byte(self):
        for percent in (100, 101, 120, 999, 1000):
            for rate in (0, 1, 128, 254, 255):
                with self.subTest(percent=percent, rate=rate):
                    self.assertIn(sdr.scaled_rate(rate, percent), range(0x100))

    def test_monotonic_in_the_multiplier(self):
        """A bigger multiplier can never yield a rarer drop."""
        for rate in SAMPLE_RATES:
            rates = [sdr.scaled_rate(rate, p) for p in (100, 120, 150, 200, 400, 1000)]
            with self.subTest(rate=rate):
                self.assertEqual(rates, sorted(rates, reverse=True))

    def test_preserves_relative_rarity_ordering(self):
        for percent in (120, 200, 400):
            scaled = [sdr.scaled_rate(r, percent) for r in SAMPLE_RATES]
            with self.subTest(percent=percent):
                self.assertEqual(scaled, sorted(scaled))

    def test_default_multiplier_is_a_real_but_gentle_change(self):
        """1.2x should move a typical rate without collapsing it to guaranteed."""
        self.assertEqual(sdr.scaled_rate(20, 120), 16)
        self.assertEqual(sdr.scaled_rate(50, 120), 41)
        self.assertGreater(sdr.scaled_rate(7, 120), 0)


class TestMultiplierWrites(unittest.TestCase):
    def test_hundred_percent_writes_nothing(self):
        rom = _fake_rom(lambda e: SAMPLE_RATES[e % len(SAMPLE_RATES)])
        self.assertEqual(sdr.build_multiplier_writes(rom, 100), {})

    def test_covers_every_enemy_with_a_scalable_rate(self):
        rom = _fake_rom(lambda e: 20)
        writes = sdr.build_multiplier_writes(rom, 200)
        self.assertEqual(len(writes), sdr.ENEMY_COUNT)
        for enemy_id in range(sdr.ENEMY_COUNT):
            self.assertEqual(writes[sdr._rate_offset(enemy_id)], bytes([sdr.scaled_rate(20, 200)]))

    def test_skips_guaranteed_and_unchanged_entries(self):
        rom = _fake_rom(lambda e: 0 if e % 2 else 20)
        writes = sdr.build_multiplier_writes(rom, 200)
        for enemy_id in range(sdr.ENEMY_COUNT):
            with self.subTest(enemy=enemy_id):
                self.assertEqual(sdr._rate_offset(enemy_id) in writes, enemy_id % 2 == 0)

    def test_reads_current_rates_not_vanilla(self):
        """It must compose with earlier edits, so it scales what is actually in the ROM."""
        rom = _fake_rom(lambda e: 5)
        writes = sdr.build_multiplier_writes(rom, 200)
        expected = sdr.scaled_rate(5, 200)
        self.assertEqual(writes[sdr._rate_offset(0)], bytes([expected]))
        self.assertNotEqual(expected, sdr.scaled_rate(20, 200))

    def test_composes_with_the_overrides(self):
        """Running the multiplier after build_writes scales the boosted rates too."""
        rom = bytearray(_fake_rom(lambda e: 32))
        for enemy_id, (_name, _old, new) in sdr.SOUL_RATE_OVERRIDES.items():
            rom[sdr._rate_offset(enemy_id)] = new
        writes = sdr.build_multiplier_writes(bytes(rom), 200)
        for enemy_id, (name, _old, new) in sdr.SOUL_RATE_OVERRIDES.items():
            with self.subTest(enemy=name):
                self.assertEqual(writes[sdr._rate_offset(enemy_id)],
                                 bytes([sdr.scaled_rate(new, 200)]))

    def test_writes_are_single_bytes_inside_the_table(self):
        rom = _fake_rom(lambda e: SAMPLE_RATES[e % len(SAMPLE_RATES)])
        table_start = sdr.ENEMY_TABLE_GBA - sdr.GBA_ROM_BASE
        table_end = table_start + sdr.ENEMY_COUNT * sdr.ENEMY_STRIDE
        for offset, data in sdr.build_multiplier_writes(rom, 150).items():
            with self.subTest(offset=offset):
                self.assertEqual(len(data), 1)
                self.assertTrue(table_start <= offset < table_end)


class TestOptionWiring(unittest.TestCase):
    def test_option_default_matches_the_requested_1_2x(self):
        from worlds.cvaos.options import MultiplySoulDropRates, SoulDropRateMultiplier

        self.assertEqual(SoulDropRateMultiplier.default, 120)
        self.assertEqual(MultiplySoulDropRates.default, 0)

    def test_named_presets_are_in_range_and_include_vanilla(self):
        from worlds.cvaos.options import SoulDropRateMultiplier as opt

        self.assertEqual(opt.special_range_names["vanilla"], 100)
        for name, value in opt.special_range_names.items():
            with self.subTest(preset=name):
                self.assertTrue(opt.range_start <= value <= opt.range_end)

    def test_range_never_makes_souls_rarer(self):
        from worlds.cvaos.options import SoulDropRateMultiplier as opt

        self.assertGreaterEqual(opt.range_start, 100)

class RateFloorTest(unittest.TestCase):
    def test_a_nonzero_rate_never_scales_to_zero(self):
        # 0 is not "very common": the death routine skips the roll for it entirely
        # (rom/soul_guarantee_hook.py), so the multiplier must never produce it.
        for rate in (1, 2, 5, 32, 0xFF):
            for percent in (150, 200, 400, 1000):
                with self.subTest(rate=rate, percent=percent):
                    self.assertGreaterEqual(sdr.scaled_rate(rate, percent), 1)


if __name__ == "__main__":
    unittest.main()
