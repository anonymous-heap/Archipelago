"""
Tests for the Enemy Checks option, which makes the first defeat of each kind of enemy a check.

    python -m pytest worlds/cvaos/test/test_enemy_checks.py -v
"""
from __future__ import annotations

import unittest
from collections import Counter
from types import SimpleNamespace
from unittest import mock

from BaseClasses import CollectionState, Entrance, MultiWorld
from test.general import setup_multiworld

from .. import CVAOSWorld, item_smoothing
from ..client_logic import ITEMS_FROM_OTHER_WORLDS, ITEMS_INCLUDING_OWN, CVAOSClientLogic
from ..data import (enemy_placements, enemy_type_by_name, enemy_type_collection,
                    item_info_collection, pickup_info_collection)
from ..enemy_checks import (ARENA_PLACEMENTS, BOSS_TYPES, FALLBACK_SOULS, SPAWNS_AFTER_BOSS_IN,
                            defeat_location_id, defeat_location_name, enemy_check_types,
                            enemy_id_for_location, has_recorded_route, placements_of)
from ..items import ENEMY_ITEM, item_table
from ..locations import defeat_location_names, location_name_to_id, pickup_location_ids
from ..options import EnemyChecks, Goal
from ..regions import can_reach_room
from ..rom.entity import GBA_ROM_BASE, PICKUP
from ..rom.patch import _ITEM_OFF, _TYPE_OFF, get_location_data, rom_gives_item
from .test_smoothing_location_settings import generate

BUILD_STEPS = ("generate_early", "create_regions", "create_items", "set_rules")
POOL_STEPS = ("generate_early", "create_regions", "create_items")


def options(enemy_checks: int, goal: int = Goal.default) -> SimpleNamespace:
    return SimpleNamespace(enemy_checks=EnemyChecks(enemy_checks), goal=Goal(goal))


def build(enemy_checks: str, steps=BUILD_STEPS, seed: int = 1, **extra) -> MultiWorld:
    return setup_multiworld(CVAOSWorld, steps, seed=seed,
                            options={"enemy_checks": enemy_checks, **extra})


def item_pool(multiworld: MultiWorld) -> Counter:
    items = [*multiworld.itempool, *multiworld.worlds[1].get_pre_fill_items()]
    return Counter(item.name for item in items)


def state_with_all_items_but(multiworld: MultiWorld, left_out: str = "") -> CollectionState:
    state = CollectionState(multiworld)
    for item in [*multiworld.itempool, *multiworld.worlds[1].get_pre_fill_items()]:
        if item.name != left_out:
            state.collect(item, prevent_sweep=True)
    return state


class EnemyTypeTableTest(unittest.TestCase):
    def test_the_table_lists_every_enemy_id_in_the_games_order(self):
        self.assertEqual([enemy.id for enemy in enemy_type_collection], list(range(113)))
        self.assertEqual([enemy_type_by_name[name].id for name in ("Belmont", "Graham", "Chaos")],
                         [110, 111, 112])

    def test_every_placement_names_a_type_from_the_table(self):
        names = {placement.name for placement in enemy_placements}
        self.assertEqual(names - set(enemy_type_by_name), set())

    def test_every_type_but_chaos_has_a_location_with_an_id_of_its_own(self):
        names = {defeat_location_name(enemy.name) for enemy in enemy_type_collection}
        self.assertEqual(names - defeat_location_names, {"Defeat Chaos"})
        ids = {location_name_to_id[name] for name in defeat_location_names}
        self.assertEqual(len(ids), 112)
        self.assertEqual(ids & pickup_location_ids, set())

    def test_a_defeat_location_id_maps_back_to_its_enemy(self):
        for enemy in enemy_type_collection:
            self.assertEqual(enemy_id_for_location(defeat_location_id(enemy.id)), enemy.id)
        for location_id in pickup_location_ids:
            self.assertIsNone(enemy_id_for_location(location_id))

    def test_boss_rush_arenas_are_not_placements(self):
        rooms = {placement.room for enemy in enemy_type_collection
                 for placement in placements_of(enemy.name)}
        self.assertEqual({room for room in rooms if room.startswith("C")}, set())
        # Man-Eater fights in a Boss Rush arena, but it is an ordinary enemy in the castle too.
        self.assertTrue(placements_of("Man-Eater"))

    def test_every_type_has_a_placement_outside_boss_rush(self):
        self.assertEqual([enemy.name for enemy in enemy_type_collection
                          if not placements_of(enemy.name)], [])


class WhichTypesTest(unittest.TestCase):
    def test_off_checks_no_types(self):
        self.assertEqual(enemy_check_types(options(EnemyChecks.option_off)), ())

    def test_bosses_checks_the_bosses(self):
        types = enemy_check_types(options(EnemyChecks.option_bosses))
        self.assertEqual({enemy.name for enemy in types}, set(BOSS_TYPES))

    def test_all_checks_every_type_but_chaos(self):
        types = enemy_check_types(options(EnemyChecks.option_all))
        self.assertEqual(len(types), 112)
        self.assertNotIn("Chaos", {enemy.name for enemy in types})

    def test_graham_is_not_a_check_when_defeating_him_is_the_goal(self):
        for setting, count in ((EnemyChecks.option_bosses, 9), (EnemyChecks.option_all, 111)):
            types = enemy_check_types(options(setting, Goal.option_graham))
            with self.subTest(setting=setting):
                self.assertEqual(len(types), count)
                self.assertNotIn("Graham", {enemy.name for enemy in types})


class GenerationTest(unittest.TestCase):
    def test_seeds_generate_beatable_with_one_defeat_location_per_type(self):
        for setting in ("bosses", "all"):
            multiworld = generate(0, {"enemy_checks": setting})
            types = enemy_check_types(multiworld.worlds[1].options)
            with self.subTest(setting=setting):
                self.assertTrue(multiworld.can_beat_game(multiworld.state))
                defeats = [location for location in multiworld.get_locations(1)
                           if location.name in defeat_location_names]
                self.assertEqual(len(defeats), len(types))

    def test_each_checked_type_adds_one_copy_of_its_item_to_the_pool(self):
        without_checks = item_pool(build("off", steps=POOL_STEPS))
        for setting in ("bosses", "all"):
            multiworld = build(setting, steps=POOL_STEPS)
            types = enemy_check_types(multiworld.worlds[1].options)
            added = Counter(ENEMY_ITEM[enemy.name] for enemy in types)
            with self.subTest(setting=setting):
                self.assertEqual(item_pool(multiworld), without_checks + added)

    def test_off_adds_no_defeat_locations(self):
        multiworld = build("off")
        self.assertEqual([location.name for location in multiworld.get_locations(1)
                          if location.name in defeat_location_names], [])


class LogicTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.multiworld = build("all")

    def can_reach(self, state: CollectionState, enemy_name: str) -> bool:
        return state.can_reach(defeat_location_name(enemy_name), "Location", 1)

    def test_a_type_without_a_route_needs_every_fallback_soul(self):
        enemy = next(enemy for enemy in enemy_check_types(self.multiworld.worlds[1].options)
                     if not has_recorded_route(enemy.name))
        self.assertTrue(self.can_reach(state_with_all_items_but(self.multiworld), enemy.name))
        for soul in FALLBACK_SOULS:
            with self.subTest(enemy=enemy.name, without=soul):
                self.assertFalse(
                    self.can_reach(state_with_all_items_but(self.multiworld, soul), enemy.name))

    def test_a_boss_without_routing_counts_once_its_arena_is_reached(self):
        # Without Giant Bat, every placement that uses the fallback stays closed, so only the
        # arena can open these checks. The arenas open one by one as the other items arrive.
        world = self.multiworld.worlds[1]
        arenas = {placement.name: placement.room for placement in enemy_placements
                  if placement.enemy_number in ARENA_PLACEMENTS}
        self.assertEqual(len(arenas), 8)
        self.assertTrue(all(has_recorded_route(boss) for boss in arenas))
        pool = [*self.multiworld.itempool, *world.get_pre_fill_items()]
        items = sorted((item for item in pool if item.advancement and item.name != "Giant Bat"),
                       key=lambda item: item.name)
        state = CollectionState(self.multiworld)
        seen = set()
        for item in [None, *items]:
            if item is not None:
                state.collect(item, prevent_sweep=True)
            for boss, room in arenas.items():
                reached = can_reach_room(state, world, room)
                seen.add((boss, reached))
                with self.subTest(boss=boss, after=item and item.name):
                    self.assertEqual(self.can_reach(state, boss), reached)
        self.assertIn(("Death", False), seen)
        self.assertIn(("Death", True), seen)

    def test_a_routed_placement_uses_its_routing_instead(self):
        # Room 003's Zombie can be reached from the start with nothing.
        self.assertTrue(has_recorded_route("Zombie"))
        self.assertTrue(self.can_reach(CollectionState(self.multiworld), "Zombie"))

    def test_placements_that_wait_for_a_boss_elsewhere_recheck_when_its_room_opens(self):
        world = self.multiworld.worlds[1]
        for room, boss_room in SPAWNS_AFTER_BOSS_IN.items():
            boss_doors = [self.multiworld.get_region(name, 1)
                          for name in world.entrance_region_names_by_room[boss_room]]
            watched = set().union(*(self.multiworld.indirect_connections.get(door, set())
                                    for door in boss_doors))
            numbers = {f"#{placement.enemy_number}" for placement in enemy_placements
                       if placement.room == room}
            entrances = [entrance for region in self.multiworld.get_regions(1)
                         for entrance in region.exits
                         if entrance.connected_region.name.startswith("Defeat: ")
                         and entrance.name.rsplit(" ", 1)[-1] in numbers]
            with self.subTest(room=room):
                self.assertTrue(entrances)
                self.assertEqual([entrance.name for entrance in entrances
                                  if entrance not in watched], [])

    def test_a_route_to_a_defeat_names_what_it_needs(self):
        # Universal Tracker's routes list each step's uses.
        state = CollectionState(self.multiworld)
        defeat_entrances = [entrance for region in self.multiworld.get_regions(1)
                            for entrance in region.exits
                            if entrance.connected_region.name.startswith("Defeat: ")]
        everything = self.multiworld.get_all_state(False)
        for entrance in defeat_entrances:
            # A way that needs nothing keeps the default rule, and no route crosses a way that
            # nothing opens.
            if (entrance.access_rule is Entrance.access_rule
                    or not entrance.access_rule(everything)):
                continue
            with self.subTest(entrance=entrance.name):
                self.assertTrue(hasattr(entrance.access_rule, "uses"))
        options = self.multiworld.worlds[1].options
        fallback_only = next(enemy.name for enemy in enemy_check_types(options)
                             if not has_recorded_route(enemy.name))
        entrance = next(entrance for entrance in defeat_entrances
                        if entrance.connected_region.name == f"Defeat: {fallback_only}")
        self.assertEqual(tuple(entrance.access_rule.uses(state)), FALLBACK_SOULS)
        room_710 = {f"#{placement.enemy_number}" for placement in enemy_placements
                    if placement.room == "710"}
        entrance = next(entrance for entrance in defeat_entrances
                        if entrance.name.rsplit(" ", 1)[-1] in room_710)
        self.assertIn("beating the boss in room 725", entrance.access_rule.uses(state))


class PriorityCapTest(unittest.TestCase):
    def test_at_most_ten_defeats_without_a_route_stay_priority(self):
        fallback_only = sorted(defeat_location_name(enemy.name)
                               for enemy in enemy_check_types(options(EnemyChecks.option_all))
                               if not has_recorded_route(enemy.name))[:15]
        routed = defeat_location_name("Zombie")
        with self.assertLogs(level="WARNING") as logs:
            multiworld = build("all", steps=("generate_early",),
                               priority_locations={*fallback_only, routed})
        priority = multiworld.worlds[1].options.priority_locations.value
        self.assertEqual(len(priority & set(fallback_only)), CVAOSWorld.MAX_FALLBACK_PRIORITY)
        self.assertIn(routed, priority)
        self.assertIn(fallback_only[-1], "\n".join(logs.output))

    def test_a_few_priority_defeats_are_left_alone(self):
        chosen = {defeat_location_name("Bat"), defeat_location_name("Skeleton")}
        multiworld = build("all", steps=("generate_early",), priority_locations=chosen)
        self.assertEqual(multiworld.worlds[1].options.priority_locations.value, chosen)


class DeliveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.with_checks = generate(3, {"enemy_checks": "all"})

    def test_slot_data_lists_exactly_the_pickups_the_rom_hands_out(self):
        world = self.with_checks.worlds[1]
        listed = set(world.fill_slot_data()["rom_given_locations"])
        expected = {location.address for location in self.with_checks.get_locations(1)
                    if location.address in pickup_location_ids and rom_gives_item(world, location)}
        self.assertEqual(listed, expected)
        self.assertTrue(listed)

    def test_the_rom_shows_an_enemy_only_item_by_its_categorys_rule(self):
        # An item that only enemies hold has no vanilla pickup to copy, so it takes the subtype
        # that vanilla pickups of its category use. Its item_offset is its id plus that
        # category's fixed amount. Accessories use the armor subtype, with an item_offset 25 more
        # than their id.
        rules = {"consumable": (2, 0), "weapon": (3, 0), "armor": (4, 0), "accessory": (4, 25)}
        info_by_name = {info.name: info for info in item_info_collection}
        held_by_a_pickup = {pickup.item_name for pickup in pickup_info_collection}
        enemy_only = set(ENEMY_ITEM.values()) - held_by_a_pickup
        world = self.with_checks.worlds[1]
        pickups = [location for location in self.with_checks.get_locations(1)
                   if location.address in pickup_location_ids]
        writes = get_location_data(world, pickups)
        placed = [location for location in pickups if location.item.name in enemy_only]
        self.assertTrue(placed)
        for location in placed:
            info = info_by_name[location.item.name]
            subtype_num, added = rules[info.item_category]
            with self.subTest(location=location.name, item=location.item.name):
                self.assertTrue(rom_gives_item(world, location))
                offset = location.address - GBA_ROM_BASE
                self.assertEqual(writes[offset + _TYPE_OFF], bytes([PICKUP, subtype_num]))
                self.assertEqual(writes[offset + _ITEM_OFF],
                                 (info.id + added).to_bytes(2, "little"))

    def test_item_smoothing_moves_items_only_between_pickups(self):
        positions = item_smoothing._run_position(self.with_checks, 1)
        self.assertTrue(positions)
        self.assertEqual([location.name for location in positions
                          if location.address not in pickup_location_ids], [])


class FakeRam:
    def __init__(self, defeated: set[int]) -> None:
        self.defeat_bytes = bytearray(16)
        for enemy_id in defeated:
            self.defeat_bytes[enemy_id >> 3] |= 1 << (enemy_id & 7)
        self.defeats_read = 0

    async def read_pickup_flags(self) -> bytes:
        return bytes(0x40)

    async def read_enemy_defeats(self) -> bytes:
        self.defeats_read += 1
        return bytes(self.defeat_bytes)

    async def cap_skull_keys(self, floor: int) -> None:
        pass


class ClientTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = CVAOSClientLogic()
        self.client._reset_session_state()

    def ctx(self, server_locations, items_handling: int = ITEMS_INCLUDING_OWN, items=(),
            slot_data=None):
        return SimpleNamespace(server_locations=set(server_locations), checked_locations=set(),
                               slot=1, items_handling=items_handling, items_received=list(items),
                               slot_data=slot_data or {}, send_msgs=mock.AsyncMock())

    async def test_each_first_defeat_the_server_tracks_is_sent(self):
        bat, zombie, chaos = (enemy_type_by_name[name].id for name in ("Bat", "Zombie", "Chaos"))
        ctx = self.ctx({defeat_location_id(bat), defeat_location_id(zombie)})
        await self.client._send_location_checks(ctx, FakeRam({bat, zombie, chaos}))
        ctx.send_msgs.assert_awaited_once_with(
            [{"cmd": "LocationChecks",
              "locations": sorted([defeat_location_id(bat), defeat_location_id(zombie)])}])

    async def test_a_seed_without_enemy_checks_never_reads_the_defeats(self):
        ram = FakeRam({enemy_type_by_name["Bat"].id})
        ctx = self.ctx(sorted(pickup_location_ids)[:5], items_handling=ITEMS_FROM_OTHER_WORLDS)
        await self.client._send_location_checks(ctx, ram)
        self.assertEqual(ram.defeats_read, 0)
        ctx.send_msgs.assert_not_awaited()

    def test_own_items_the_rom_gave_are_not_given_again(self):
        from NetUtils import NetworkItem
        pickup, other_pickup = sorted(pickup_location_ids)[:2]
        defeat = defeat_location_id(enemy_type_by_name["Bat"].id)
        code = item_table["Potion"].code
        given_by_rom = NetworkItem(code, pickup, 1)
        at_a_defeat = NetworkItem(code, defeat, 1)
        from_another_world = NetworkItem(code, pickup, 2)
        own_copy_not_from_rom = NetworkItem(code, other_pickup, 1)
        ctx = self.ctx((), items=[given_by_rom, at_a_defeat, from_another_world,
                                  own_copy_not_from_rom],
                       slot_data={"rom_given_locations": [pickup],
                                  "starting_inventory_from_server": True})
        self.assertEqual(CVAOSClientLogic._grantable_items(ctx),
                         [at_a_defeat, from_another_world, own_copy_not_from_rom])

    def test_the_rom_given_list_is_ignored_when_own_items_are_not_requested(self):
        from NetUtils import NetworkItem
        pickup = min(pickup_location_ids)
        item = NetworkItem(item_table["Potion"].code, pickup, 1)
        ctx = self.ctx((), items_handling=ITEMS_FROM_OTHER_WORLDS, items=[item],
                       slot_data={"rom_given_locations": [pickup],
                                  "starting_inventory_from_server": True})
        self.assertEqual(CVAOSClientLogic._grantable_items(ctx), [item])


if __name__ == "__main__":
    unittest.main()
