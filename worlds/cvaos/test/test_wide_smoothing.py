"""
Tests for Wide Item Smoothing (wide_smoothing.py).

The seeds here go through Main.main, so locality, exclusion, plando and core's own progression
balancing all take effect. The tests assert the mechanisms rather than rates. By default they
generate a duo of two Aria of Sorrow slots. With CVAOS_WIDE_SCENARIOS=1 they also generate 30 solo
preset seeds and 10 seeds each of a Hollow Knight duo, a Dark Souls III duo and a duo whose Aria of
Sorrow slot keeps weapons local and armor non-local.

    python -m pytest worlds/cvaos/test/test_wide_smoothing.py -v
"""
from __future__ import annotations

import hashlib
import os
import unittest
from argparse import Namespace
from collections import Counter
from unittest import mock

import Fill
import Main
from BaseClasses import ItemClassification, MultiWorld, PlandoOptions
from test.general import setup_multiworld
from worlds.AutoWorld import AutoWorldRegister, World

from .. import CVAOSWorld, seed_depth, wide_smoothing
from ..data.item_balancing import worth
from ..drop_shuffle import (DropSlot, _supply, plan_drop_shuffle, plan_travel,
                            vanilla_drop_table)
from ..drop_sources import final_drop_table, travel_plan
from ..items import logic_only_progression_names
from ..locations import CVAOSLocation
from ..wide_smoothing import (BAND, RUNGS, OrderResult, _is_defeat, promote_mid_game_gear,
                              qualifies)

GAME = CVAOSWorld.game
PRESET = {"enemy_checks": "all", "soul_shuffle": "matched", "enemy_drop_rate": 350,
          "wide_item_smoothing": True}
SCENARIOS = bool(os.environ.get("CVAOS_WIDE_SCENARIOS"))


def generate(seed: int, players: list[tuple[str, dict]],
             skip_balancing: bool = False) -> MultiWorld:
    """Generate a multiworld through Main.main, without output, and return it.

    Each player is a game name and that player's options. Options left out take their defaults.
    """
    args = Namespace(multi=len(players), race=False, outputname=None, outputpath=None, spoiler=0,
                     plando=PlandoOptions.from_set({"items"}), csv_output=False, skip_output=True,
                     spoiler_only=False, skip_prog_balancing=skip_balancing,
                     game={}, name={}, sprite={}, sprite_pool={})
    for player, (game, options) in enumerate(players, 1):
        args.game[player] = game
        args.name[player] = f"{game.split()[0]}{player}"
        args.sprite[player] = None
        args.sprite_pool[player] = []
        for key, option in AutoWorldRegister.world_types[game].options_dataclass.type_hints.items():
            if not hasattr(args, key):
                setattr(args, key, {})
            value = option.from_any(options.get(key, option.default))
            value.verify(AutoWorldRegister.world_types[game], args.name[player], args.plando)
            getattr(args, key)[player] = value
    return Main.main(args, seed)


# How early each rung's gear should be held: by a fifth of the run for the first weapon, and by
# 0.35, about a third, for the other weapon and the armor.
RUNG_DEPTHS = (0.2, 0.35, 0.35)


def rungs_held(multiworld: MultiWorld) -> dict[int, tuple[bool, ...]]:
    """Return, for each player with Wide on, whether they hold each rung's gear in time.

    Positions are on the logic clock, and the player's items count wherever they sit.
    """
    sweep = wide_smoothing.walk(multiworld, room_clock=False)
    held = {}
    for world in multiworld.get_game_worlds(GAME):
        if world.options.wide_item_smoothing:
            clock = wide_smoothing.Clock(multiworld, world.player, sweep)
            items = [(location.item, clock.position(location))
                     for location in multiworld.get_filled_locations()
                     if location.item.player == world.player]
            held[world.player] = tuple(
                any(position is not None and position <= depth and qualifies(item, rung)
                    for item, position in items)
                for rung, depth in zip(RUNGS, RUNG_DEPTHS))
    return held


def placement_hash(multiworld: MultiWorld) -> str:
    rows = sorted(f"{loc.player}|{loc.name}|{loc.item.player}|{loc.item.name}"
                  for loc in multiworld.get_filled_locations())
    return hashlib.sha1("\n".join(rows).encode("utf-8")).hexdigest()[:16]


class WideOffTest(unittest.TestCase):
    # Placement hashes from the world as it was before Wide Item Smoothing existed. With the option
    # off, the same seed and options place every item as they did then.
    BEFORE_WIDE = (
        (2, [(GAME, {"enemy_checks": "all", "item_smoothing_order": "spheres"})],
         "b344c1c3fcf61cdc"),
        (4, [(GAME, {"enemy_checks": "all"}), (GAME, {"item_smoothing": "loose"})],
         "a4b0d8ade57b654c"),
    )

    def test_seeds_with_wide_off_place_items_as_before(self):
        for seed, players, expected in self.BEFORE_WIDE:
            with self.subTest(seed=seed):
                self.assertEqual(placement_hash(generate(seed, players)), expected)


class PoolTest(unittest.TestCase):
    def test_mid_game_filler_gear_becomes_useful(self):
        world = setup_multiworld(CVAOSWorld, (), seed=1).worlds[1]
        pool = [world.create_item(name) for name in ("Samurai Armor", "Gungner", "Copper Plate",
                                                     "Potion")]
        promote_mid_game_gear(pool)
        self.assertEqual([item.classification for item in pool],
                         [ItemClassification.useful, ItemClassification.useful,
                          world.create_item("Copper Plate").classification,
                          ItemClassification.filler])


class TradeTest(unittest.TestCase):
    """The order's step for a location that accepts no remaining item."""

    def deal(self, accepted: list[set[str]]) -> tuple[bool, list[CVAOSLocation], OrderResult]:
        """Deal a Potion and a High Potion to locations that accept only the named items."""
        multiworld = setup_multiworld(CVAOSWorld, (), seed=1)
        locations = []
        for index, names in enumerate(accepted):
            location = CVAOSLocation(1, f"Test {index}")
            location.item_rule = lambda item, names=names: item.name in names
            locations.append(location)
        positions = {location: 0.1 * index for index, location in enumerate(locations, 1)}
        pool = [multiworld.worlds[1].create_item(name) for name in ("Potion", "High Potion")]
        result = OrderResult(positions)
        dealt = wide_smoothing._deal(locations, pool, positions, multiworld.state, result)
        return dealt, locations, result

    def test_a_location_that_fits_no_remaining_item_trades_with_an_earlier_one(self):
        dealt, locations, result = self.deal([{"Potion", "High Potion"}, {"Potion"}])
        self.assertTrue(dealt)
        self.assertEqual([location.item.name for location in locations], ["High Potion", "Potion"])
        self.assertEqual(result.trades, 1)

    def test_the_deal_fails_when_no_pair_of_locations_accepts_the_trade(self):
        dealt, _locations, _result = self.deal([{"Potion"}, {"Potion"}])
        self.assertFalse(dealt)

    def test_an_order_that_fails_to_deal_is_undone(self):
        multiworld = generate(3, [(GAME, PRESET)])
        before = placement_hash(multiworld)

        def scramble_and_fail(locations, pool, *_args):
            for location, item in zip(locations, reversed(pool)):
                location.item = item
                item.location = location
            return False

        with mock.patch.object(wide_smoothing, "_deal", side_effect=scramble_and_fail):
            result = wide_smoothing._order_player(
                multiworld, multiworld.worlds[1], wide_smoothing.walk(multiworld, room_clock=True),
                wide_smoothing.walk(multiworld, room_clock=False))
        self.assertTrue(result.undone)
        self.assertEqual(placement_hash(multiworld), before)


class BridgeTest(unittest.TestCase):
    """The bridge: Wide's own calls to core's progression balancing, and their guards."""

    PLAYERS = [(GAME, {"wide_item_smoothing": True}), (GAME, {"wide_item_smoothing": True})]

    def generate_counting(self, players=None, skip_balancing: bool = False, **patches):
        """Generate a small duo. Return its log lines and how often Wide balanced and walked."""
        balance = mock.Mock(wraps=Fill.balance_multiworld_progression)
        walk = mock.Mock(wraps=wide_smoothing.walk)
        with mock.patch.object(Fill, "balance_multiworld_progression", balance), \
                mock.patch.object(wide_smoothing, "walk", walk), \
                self.assertLogs(level="INFO") as logs:
            for name, value in patches.items():
                stack = mock.patch.object(wide_smoothing, name, **value)
                stack.start()
                self.addCleanup(stack.stop)
            multiworld = generate(5, players or self.PLAYERS, skip_balancing)
        lines = [line for line in logs.output if f"{GAME}: Wide Item Smoothing" in line]
        return multiworld, lines, balance.call_count, walk.call_count

    def test_a_multiworld_balances_three_times_with_one_walk_per_round_for_every_player(self):
        multiworld, lines, calls, walks = self.generate_counting()
        self.assertEqual(calls, wide_smoothing.ROUNDS)
        # Each round walks the room clock and logic once, for both players, and the floor walks
        # logic once more.
        self.assertEqual(walks, 2 * wide_smoothing.ROUNDS + 1)
        self.assertEqual(len(lines), 1)
        self.assertIn("runs progression balancing 3 times for Castlevania1, Castlevania2", lines[0])
        self.assertTrue(all(len(world.wide_report.orders) == wide_smoothing.ROUNDS
                            for world in multiworld.worlds.values()))

    def test_no_calls_when_the_host_skips_balancing(self):
        multiworld, lines, calls, _walks = self.generate_counting(skip_balancing=True)
        self.assertEqual(calls, 0)
        self.assertIn("the host skips progression balancing", lines[0])
        self.assertEqual(len(multiworld.worlds[1].wide_report.orders), 1)

    def test_no_calls_when_no_player_balances(self):
        quiet = {"wide_item_smoothing": True, "progression_balancing": 0}
        _multiworld, lines, calls, _walks = self.generate_counting([(GAME, quiet), (GAME, quiet)])
        self.assertEqual(calls, 0)
        self.assertIn("no player has progression balancing on", lines[0])

    def test_the_host_setting_turns_the_calls_off(self):
        _multiworld, lines, calls, _walks = self.generate_counting(
            _host_allows_balancing=dict(return_value=False))
        self.assertEqual(calls, 0)
        self.assertIn("wide_smoothing_runs_balancing is off", lines[0])
        self.assertIn("may bring mid-game gear", lines[0])

    def test_a_failed_import_orders_once(self):
        multiworld, lines, calls, _walks = self.generate_counting(
            _import_balancing=dict(side_effect=ImportError))
        self.assertEqual(calls, 0)
        self.assertIn("could not import progression balancing", lines[0])
        self.assertEqual(len(multiworld.worlds[1].wide_report.orders), 1)

    def test_a_type_error_from_balancing_orders_once(self):
        def changed_signature(multiworld):
            raise TypeError("balance_multiworld_progression() takes 2 arguments")
        with mock.patch.object(Fill, "balance_multiworld_progression", changed_signature), \
                self.assertLogs(level="WARNING"):
            multiworld = generate(5, self.PLAYERS)
        self.assertEqual(len(multiworld.worlds[1].wide_report.orders), 1)

    def test_outside_main_the_skip_flag_comes_from_the_command_line(self):
        self.assertFalse(wide_smoothing.host_skips_balancing())
        with mock.patch.object(wide_smoothing.sys, "argv", ["Generate.py", "--skip_prog"]):
            self.assertTrue(wide_smoothing.host_skips_balancing())


class PostBalancingHookTest(unittest.TestCase):
    """The path for a core that calls a hook on every world after progression balancing."""

    def test_with_the_hook_wide_orders_once_there_and_never_balances(self):
        balance = mock.Mock(wraps=Fill.balance_multiworld_progression)
        with mock.patch.object(World, "post_balancing", lambda self, *args: None, create=True), \
                mock.patch.object(Fill, "balance_multiworld_progression", balance):
            multiworld = generate(5, BridgeTest.PLAYERS)
            self.assertIsNone(getattr(multiworld.worlds[1], "wide_report", None))
            CVAOSWorld.stage_post_balancing(multiworld, True)
        self.assertEqual(balance.call_count, 0)
        for world in multiworld.worlds.values():
            self.assertEqual(len(world.wide_report.orders), 1)
            self.assertIsNotNone(world.wide_report.floor)

    def test_without_the_hook_wide_runs_in_stage_post_fill(self):
        self.assertFalse(wide_smoothing.core_has_post_balancing())


class DropBandTest(unittest.TestCase):
    """The band on the enemy drop table."""

    SUPPLY = ("Potion", "High Potion", "Rapier")

    @staticmethod
    def table(rows: dict[int, tuple[str, str]]) -> dict[int, tuple[DropSlot, DropSlot]]:
        return {enemy_id: tuple(DropSlot(item or None, 10 * enemy_id + slot)
                                for slot, item in enumerate(row, start=1))
                for enemy_id, row in rows.items()}

    def test_on_the_vanilla_table_every_drop_ends_under_its_enemys_line(self):
        multiworld = generate(1, [(GAME, PRESET)])
        depths = seed_depth.enemy_seed_depths(multiworld.worlds[1])
        before = vanilla_drop_table()
        # Any supply works here. This one is the preset's.
        band = wide_smoothing.band_drop_table(before, depths, plan_travel(2, 0, True).supply)
        self.assertGreater(band.swaps, 0)
        self.assertEqual(set(band.table), set(before))
        for enemy_id, row in band.table.items():
            for slot, content in enumerate(row):
                with self.subTest(enemy_id=enemy_id, slot=slot):
                    self.assertEqual(content.item is None, before[enemy_id][slot].item is None)
                    if content.item:
                        self.assertLessEqual(worth.item_worth(content.item),
                                             depths[enemy_id] + BAND + 1e-9)
        # Each drop moved whole, with its rarity byte, unless the supply replaced it.
        old = Counter(slot for row in before.values() for slot in row if slot.item)
        new = Counter(slot for row in band.table.values() for slot in row if slot.item)
        self.assertEqual(sum((old - new).values()), len(band.replaced))
        changed = sum(1 for enemy_id, row in band.table.items()
                      for slot, content in enumerate(row) if content != before[enemy_id][slot])
        self.assertLessEqual(changed, 2 * band.swaps + len(band.replaced))

    def test_the_patch_writes_the_banded_table_with_or_without_item_drop_shuffle(self):
        for shuffle in (False, True):
            world = generate(1, [(GAME, {**PRESET, "item_drop_shuffle": shuffle})]).worlds[1]
            depths = seed_depth.enemy_seed_depths(world)
            if shuffle:
                plan = plan_drop_shuffle(2, 0, True, depths)
                start, supply = plan.table, plan.travel.supply
            else:
                start, supply = vanilla_drop_table(), _supply(frozenset())
            table = final_drop_table(world)
            with self.subTest(item_drop_shuffle=shuffle):
                self.assertEqual(table, wide_smoothing.band_drop_table(start, depths, supply).table)
                if not shuffle:
                    # The game's own table has early drops above the band, so it changes.
                    self.assertNotEqual(dict(table), dict(start))

    def test_with_every_check_wide_moves_every_drop_but_the_strongest(self):
        options = {"enemy_checks": "all", "item_drop_shuffle": True, "wide_item_smoothing": True}
        world = setup_multiworld(CVAOSWorld, ("generate_early",), seed=1, options=options).worlds[1]
        travelling = travel_plan(world).travelling
        self.assertEqual(len(travelling), 85)
        self.assertTrue(all(worth.item_worth(drop.item) < worth.STRONG_WORTH
                            for drop in travelling))

    def test_a_drop_trades_with_the_earliest_slot_of_its_kind_that_admits_it(self):
        table = self.table({0: ("Battle Axe", ""), 1: ("Potion", ""), 2: ("Short Sword", ""),
                            3: ("Leather Plate", ""), 4: ("", "Bamboo Sword")})
        depths = {0: 0.0, 1: 0.6, 2: 0.7, 3: 0.65, 4: 0.6}
        band = wide_smoothing.band_drop_table(table, depths, self.SUPPLY)
        # Enemy 1's Potion is earlier but a consumable, and enemy 4's slot is a rare one.
        self.assertEqual(band.table[3][0], DropSlot("Battle Axe", 1))
        self.assertEqual(band.table[0][0], DropSlot("Leather Plate", 31))
        self.assertEqual((band.swaps, band.replaced), (1, ()))

    def test_a_drop_no_slot_admits_gives_way_to_the_best_supply_item_under_the_line(self):
        band = wide_smoothing.band_drop_table(self.table({0: ("Final Sword", "Potion")}), {0: 0.0},
                                              self.SUPPLY)
        self.assertEqual(band.table[0], (DropSlot("High Potion", 1), DropSlot("Potion", 2)))
        self.assertEqual(band.replaced, (((0, 1), "Final Sword", "High Potion"),))


class WideScenario:
    """The mechanisms every scenario asserts. A subclass names its players and seeds."""

    PLAYERS: list[tuple[str, dict]] = []
    SEEDS: tuple[int, ...] = ()
    runs: list[MultiWorld]

    rungs_before_core: list[dict[int, tuple[bool, ...]]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.runs, cls.rungs_before_core = [], []
        balance = Main.balance_multiworld_progression
        for seed in cls.SEEDS:
            held = {}

            def note_rungs_then_balance(multiworld):
                held.update(rungs_held(multiworld))
                balance(multiworld)

            with mock.patch.object(Main, "balance_multiworld_progression",
                                   side_effect=note_rungs_then_balance):
                multiworld = generate(seed, cls.PLAYERS)
            cls.runs.append(multiworld)
            # A solo seed has no core balancing, so its rungs are read at the end.
            cls.rungs_before_core.append(held or rungs_held(multiworld))

    def wide_worlds(self):
        for multiworld in self.runs:
            for world in multiworld.get_game_worlds(GAME):
                if world.options.wide_item_smoothing:
                    yield multiworld, world

    def test_every_unlocked_location_accepts_its_item(self):
        # Wide moves only its own players' items. Another world's own smoothing, such as Dark Souls
        # III's, may leave that world's items where its own rules reject them.
        for multiworld in self.runs:
            moved = {world.player for _multiworld, world in self.wide_worlds()
                     if _multiworld is multiworld}
            broken = [f"{loc.player}:{loc.name} <- {loc.item.name}"
                      for loc in multiworld.get_filled_locations()
                      if loc.item.player in moved and not loc.locked
                      and not loc.can_fill(multiworld.state, loc.item, False)]
            self.assertEqual(broken, [], multiworld.seed)

    def test_equal_seeds_give_equal_placements(self):
        self.assertEqual(placement_hash(generate(self.SEEDS[0], self.PLAYERS)),
                         placement_hash(self.runs[0]))

    def test_every_rung_is_met_before_core_balancing(self):
        # The floor can miss a rung only when no qualifying item can reach an early filler
        # location, and it records each miss.
        for multiworld, held in zip(self.runs, self.rungs_before_core):
            self.assertTrue(held)
            for player, rungs in held.items():
                missed = multiworld.worlds[player].wide_report.floor.missed
                for rung, met in zip(RUNGS, rungs):
                    self.assertTrue(met or rung in missed, (multiworld.seed, player, rung))

    def assert_every_rung_held(self):
        for multiworld, held in zip(self.runs, self.rungs_before_core):
            for player, rungs in held.items():
                self.assertEqual(rungs, (True, True, True), (multiworld.seed, player))

    def test_pinned_locations_hold_breakers(self):
        for multiworld, world in self.wide_worlds():
            for order in world.wide_report.orders:
                pinned = [order.placed[loc].name for loc in order.pinned]
                self.assertTrue(all(name in logic_only_progression_names for name in pinned),
                                (multiworld.seed, pinned))

    def test_the_restore_never_fires(self):
        for multiworld, world in self.wide_worlds():
            self.assertEqual(world.wide_report.restores, 0, multiworld.seed)

    def test_nothing_sits_above_the_band_except_counted_placements(self):
        for multiworld, world in self.wide_worlds():
            for order in world.wide_report.orders:
                above = [loc.name for loc, item in order.placed.items()
                         if worth.item_worth(item.name) > order.positions[loc] + BAND]
                self.assertLessEqual(len(above), order.over_band, (multiworld.seed, above))


class WideDuoTest(WideScenario, unittest.TestCase):
    PLAYERS = [(GAME, PRESET), (GAME, PRESET)]
    SEEDS = (1,)

    def test_the_order_covers_pickups_defeats_and_items_abroad(self):
        for _multiworld, world in self.wide_worlds():
            dealt = world.wide_report.orders[-1].positions
            self.assertTrue(any(loc.player == world.player and _is_defeat(loc) for loc in dealt))
            self.assertTrue(any(loc.player == world.player and not _is_defeat(loc)
                                for loc in dealt))
            self.assertTrue(any(loc.player != world.player for loc in dealt))

    def test_every_player_holds_every_rung(self):
        self.assert_every_rung_held()

    def test_the_pin_keeps_a_breaker_where_logic_first_finds_one(self):
        self.assertTrue(any(order.pinned for _multiworld, world in self.wide_worlds()
                            for order in world.wide_report.orders))


def scenario(cls):
    return unittest.skipUnless(SCENARIOS, "set CVAOS_WIDE_SCENARIOS=1 to run")(cls)


@scenario
class SoloPresetTest(WideScenario, unittest.TestCase):
    PLAYERS = [(GAME, PRESET)]
    SEEDS = tuple(range(100, 130))

    def test_every_player_holds_every_rung(self):
        self.assert_every_rung_held()


@scenario
class HollowKnightDuoTest(WideScenario, unittest.TestCase):
    PLAYERS = [(GAME, PRESET), ("Hollow Knight", {})]
    SEEDS = tuple(range(200, 210))


@scenario
class DarkSoulsDuoTest(WideScenario, unittest.TestCase):
    PLAYERS = [(GAME, PRESET), ("Dark Souls III", {})]
    SEEDS = tuple(range(300, 310))


@scenario
class LocalityDuoTest(WideScenario, unittest.TestCase):
    PLAYERS = [(GAME, {**PRESET, "local_items": ["Weapons"], "non_local_items": ["Armor"]}),
               ("Hollow Knight", {})]
    SEEDS = tuple(range(400, 410))


if __name__ == "__main__":
    unittest.main()
