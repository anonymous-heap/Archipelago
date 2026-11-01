"""
Tests for the Progressive Movement option.

With the option on, three Progressive Movement items replace the Flying Armor, Malphas and Giant Bat
souls. The n-th copy obtained counts as the n-th of those souls, both in logic and in the game.

    python -m pytest worlds/cvaos/test/test_progressive_movement.py -v
"""
from __future__ import annotations

import statistics
import unittest
from collections import Counter
from types import SimpleNamespace

from BaseClasses import CollectionState, MultiWorld
from Fill import distribute_items_restrictive
from test.general import gen_steps, setup_multiworld
from worlds.AutoWorld import call_all

from .. import CVAOSWorld, item_granting, item_smoothing
from ..client_logic import PROGRESSIVE_MOVEMENT_CODE, CVAOSClientLogic
from ..data import item_info_collection
from ..items import PROGRESSIVE_MOVEMENT, PROGRESSIVE_MOVEMENT_STAGES, item_table
from ..rom.entity import GBA_ROM_BASE
from ..rom.patch import _AP_PLACEHOLDER, _ITEM_OFF, _TYPE_OFF, get_location_data

STEPS = ("generate_early", "create_regions", "create_items", "set_rules")
MALPHAS = next(info for info in item_info_collection if info.name == "Malphas")
OTHER_ITEM_CODE = item_table["Potion (A)"].code


def build(progressive: bool, steps=STEPS, seed: int = 5, **options) -> MultiWorld:
    return setup_multiworld(CVAOSWorld, steps, seed=seed,
                            options={"progressive_movement": progressive, **options})


def items_of(multiworld: MultiWorld) -> list:
    # The pool plus the items item smoothing holds back for pre_fill.
    return [*multiworld.itempool, *multiworld.worlds[1].get_pre_fill_items()]


def state_with(multiworld: MultiWorld, names: list[str]) -> CollectionState:
    state = CollectionState(multiworld)
    for name in names:
        state.collect(multiworld.worlds[1].create_item(name), True)
    state.sweep_for_advancements()
    return state


def reachable(multiworld: MultiWorld, names: list[str]) -> frozenset[str]:
    state = state_with(multiworld, names)
    return frozenset(loc.name for loc in multiworld.get_locations(1) if loc.can_reach(state))


class PoolTest(unittest.TestCase):
    def test_three_copies_replace_the_three_souls(self):
        on = Counter(item.name for item in items_of(build(True)))
        off = Counter(item.name for item in items_of(build(False)))
        self.assertEqual(on[PROGRESSIVE_MOVEMENT], 3)
        for name in PROGRESSIVE_MOVEMENT_STAGES:
            self.assertEqual((on[name], off[name]), (0, 1), name)
        self.assertEqual(sum(on.values()), sum(off.values()))
        self.assertEqual(off[PROGRESSIVE_MOVEMENT], 0)


class CollectTest(unittest.TestCase):
    def test_each_copy_counts_as_the_next_soul_and_removing_it_takes_that_soul_away(self):
        multiworld = build(True)
        world = multiworld.worlds[1]
        state = CollectionState(multiworld)
        copies = [world.create_item(PROGRESSIVE_MOVEMENT) for _ in range(3)]
        for count, item in enumerate(copies, start=1):
            state.collect(item, True)
            owned = [name for name in PROGRESSIVE_MOVEMENT_STAGES if state.has(name, 1)]
            self.assertEqual(owned, list(PROGRESSIVE_MOVEMENT_STAGES[:count]))
        for count, item in zip((2, 1, 0), reversed(copies)):
            state.remove(item)
            owned = [name for name in PROGRESSIVE_MOVEMENT_STAGES if state.has(name, 1)]
            self.assertEqual(owned, list(PROGRESSIVE_MOVEMENT_STAGES[:count]))

    def test_k_copies_reach_what_the_first_k_souls_reach(self):
        progressive, plain = build(True), build(False)
        others = sorted({item.name for item in items_of(plain) if item.advancement}
                        - set(PROGRESSIVE_MOVEMENT_STAGES))
        for k in range(4):
            for extra in (others[:0], others[:len(others) // 2], others):
                with self.subTest(copies=k, other_items=len(extra)):
                    self.assertEqual(
                        reachable(progressive, [PROGRESSIVE_MOVEMENT] * k + extra),
                        reachable(plain, list(PROGRESSIVE_MOVEMENT_STAGES[:k]) + extra))

    def test_a_copy_gives_nothing_while_the_option_is_off(self):
        # With the option off, the client grants no soul for a copy, so logic must not count one.
        multiworld = build(False)
        state = CollectionState(multiworld)
        state.collect(multiworld.worlds[1].create_item(PROGRESSIVE_MOVEMENT), True)
        self.assertFalse(state.has("Flying Armor", 1))

    def test_another_players_copy_gives_this_player_nothing(self):
        multiworld = setup_multiworld([CVAOSWorld, CVAOSWorld], STEPS, seed=4,
                                      options={"progressive_movement": True})
        state = CollectionState(multiworld)
        state.collect(multiworld.worlds[2].create_item(PROGRESSIVE_MOVEMENT), True)
        self.assertTrue(state.has("Flying Armor", 2))
        self.assertFalse(state.has("Flying Armor", 1))


class GenerationTest(unittest.TestCase):
    def test_filled_seeds_place_every_copy_and_write_them_as_the_placeholder(self):
        for smoothing in ("strict", "off"):
            for seed in (1, 2, 3):
                with self.subTest(item_smoothing=smoothing, seed=seed):
                    multiworld = build(True, steps=gen_steps, seed=seed, item_smoothing=smoothing)
                    distribute_items_restrictive(multiworld)
                    call_all(multiworld, "post_fill")
                    self.assertTrue(multiworld.can_beat_game(multiworld.get_all_state()))
                    world = multiworld.worlds[1]
                    locations = [loc for loc in multiworld.get_locations(1) if loc.address is not None]
                    writes = get_location_data(world, locations)
                    copies = [loc for loc in locations if loc.item.name == PROGRESSIVE_MOVEMENT]
                    self.assertEqual(len(copies), 3)
                    for loc in copies:
                        offset = loc.address - GBA_ROM_BASE
                        written = (writes[offset + _TYPE_OFF][0], writes[offset + _TYPE_OFF][1],
                                   int.from_bytes(writes[offset + _ITEM_OFF], "little"))
                        self.assertEqual(written, _AP_PLACEHOLDER)
                    slot_data = world.fill_slot_data()
                    self.assertEqual(slot_data["progressive_movement_locations"],
                                     sorted(loc.address for loc in copies))
                    self.assertEqual(slot_data["progressive_movement_starting"], 0)

    def test_starting_copies_are_counted_in_slot_data(self):
        multiworld = build(True)
        world = multiworld.worlds[1]
        # What generation does with a start_inventory entry.
        multiworld.push_precollected(world.create_item(PROGRESSIVE_MOVEMENT))
        self.assertEqual(world.fill_slot_data()["progressive_movement_starting"], 1)

    def test_slot_data_lists_only_this_players_own_copies(self):
        # Smoothing is off here because it would pre-place every copy in its own world. The main
        # fill can then put copies in the other player's world.
        multiworld = setup_multiworld([CVAOSWorld, CVAOSWorld], gen_steps, seed=4,
                                      options={"progressive_movement": True,
                                               "item_smoothing": "off"})
        distribute_items_restrictive(multiworld)
        foreign = [loc for loc in multiworld.get_filled_locations()
                   if loc.item.name == PROGRESSIVE_MOVEMENT and loc.item.player != loc.player]
        self.assertTrue(foreign, "the seed must put a copy in the other world to test this")
        for player in (1, 2):
            with self.subTest(player=player):
                own = sorted(loc.address for loc in multiworld.get_locations(player)
                             if loc.item.name == PROGRESSIVE_MOVEMENT and loc.item.player == player)
                slot_data = multiworld.worlds[player].fill_slot_data()
                listed = slot_data["progressive_movement_locations"]
                self.assertEqual(listed, own)


class SmoothingTest(unittest.TestCase):
    """With Malphas as the early soul, all three copies are pre-placed, and the two that stand in
    for Malphas are aimed early. The second copy obtained, which gives double jump, then usually
    lands in the early band."""

    def test_two_copies_land_early_when_malphas_is_the_early_soul(self):
        from unittest import mock
        second_copy = []
        with mock.patch.object(item_smoothing, "_designate_early_soul", return_value="Malphas"):
            for seed in (1, 2, 3, 4):
                multiworld = build(True, steps=gen_steps, seed=seed,
                                   early_mobility_souls="guarantee_early")
                placed = [loc for loc in multiworld.get_locations(1)
                          if loc.item is not None and loc.item.name == PROGRESSIVE_MOVEMENT]
                self.assertEqual(len(placed), 3, f"seed {seed}: every copy is pre-placed")
                distribute_items_restrictive(multiworld)
                call_all(multiworld, "post_fill")
                position = item_smoothing._run_position(multiworld, 1)
                copies = sorted(p for loc, p in position.items()
                                if loc.item.name == PROGRESSIVE_MOVEMENT)
                second_copy.append(copies[1])
        self.assertLess(statistics.median(second_copy), item_smoothing.EARLY_BAND, second_copy)


class FakeRam:
    """A stand-in for the game's RAM that holds soul counts and records each soul granted.

    ``ready`` is what the patched ``announce.is_ready`` returns: whether the announcement slot is
    free."""

    def __init__(self, owned: set[str] = frozenset(), ready: bool = True) -> None:
        self.owned = {name: 1 for name in owned}
        self.granted: list[str] = []
        self.ready = ready

    def _name(self, category: str, index: int) -> str:
        return next(info.name for info in item_info_collection
                    if info.item_category == category and info.id == index)

    async def owned_count(self, category: str, index: int) -> int:
        return self.owned.get(self._name(category, index), 0)

    async def give_item(self, category: str, index: int) -> bool:
        name = self._name(category, index)
        self.owned[name] = self.owned.get(name, 0) + 1
        self.granted.append(name)
        return True


class ClientGrantTest(unittest.IsolatedAsyncioTestCase):
    LOCAL_COPIES = [101, 102]

    def setUp(self) -> None:
        from unittest import mock
        self.client = CVAOSClientLogic()
        self.client._reset_session_state()
        announce = mock.patch("worlds.cvaos.announce.announce_item_number", new=mock.AsyncMock())
        ready = mock.patch("worlds.cvaos.announce.is_ready",
                           new=mock.AsyncMock(side_effect=lambda ram: ram.ready))
        self.announce = announce.start()
        ready.start()
        self.addCleanup(announce.stop)
        self.addCleanup(ready.stop)

    def ctx(self, received_copies: int = 0, checked=(), enabled: bool = True, starting: int = 0,
            other_items: int = 0):
        items = ([SimpleNamespace(item=PROGRESSIVE_MOVEMENT_CODE, location=1)] * received_copies
                 + [SimpleNamespace(item=OTHER_ITEM_CODE, location=1)] * other_items
                 + [SimpleNamespace(item=PROGRESSIVE_MOVEMENT_CODE, location=-2)] * starting)
        return SimpleNamespace(
            slot_data={"progressive_movement": enabled, "progressive_movement_starting": starting,
                       "progressive_movement_locations": self.LOCAL_COPIES},
            items_received=items, checked_locations=set(checked))

    async def ticks(self, ctx, ram: FakeRam, count: int = 5) -> None:
        for _ in range(count):
            await self.client._grant_progressive_movement(ctx, ram)

    async def test_received_and_found_copies_both_count(self):
        ram = FakeRam()
        await self.ticks(self.ctx(received_copies=1, checked=[102, 999]), ram)
        self.assertEqual(ram.granted, ["Flying Armor", "Malphas"])
        self.announce.assert_awaited_with(ram, MALPHAS.item_number, new_soul_pause=True)

    async def test_only_copies_are_counted_among_received_items(self):
        ram = FakeRam()
        await self.ticks(self.ctx(received_copies=1, other_items=4), ram)
        self.assertEqual(ram.granted, ["Flying Armor"])

    async def test_starting_copies_count_once_even_when_the_server_also_sends_them(self):
        # The ctx also lists the starting copy among the received items, as the server sends it to
        # a client that asks for its starting inventory. The copy must still count only once.
        ram = FakeRam()
        await self.ticks(self.ctx(starting=1), ram)
        self.assertEqual(ram.granted, ["Flying Armor"])

    async def test_a_location_known_both_ways_counts_once(self):
        self.client.local_checked_locations.add(101)
        ram = FakeRam()
        await self.ticks(self.ctx(checked=[101]), ram)
        self.assertEqual(ram.granted, ["Flying Armor"])

    async def test_a_copy_checked_this_session_counts_before_the_server_confirms_it(self):
        self.client.local_checked_locations.add(101)
        ram = FakeRam()
        await self.ticks(self.ctx(), ram)
        self.assertEqual(ram.granted, ["Flying Armor"])

    async def test_one_soul_per_tick(self):
        ram = FakeRam()
        await self.ticks(self.ctx(received_copies=3), ram, count=1)
        self.assertEqual(ram.granted, ["Flying Armor"])

    async def test_waits_while_the_announcement_slot_is_busy(self):
        ram = FakeRam(ready=False)
        await self.ticks(self.ctx(received_copies=3), ram)
        self.assertEqual(ram.granted, [])
        ram.ready = True
        await self.ticks(self.ctx(received_copies=3), ram, count=1)
        self.assertEqual(ram.granted, ["Flying Armor"])

    async def test_a_soul_already_owned_is_not_granted_again(self):
        ram = FakeRam(owned={"Flying Armor"})
        await self.ticks(self.ctx(received_copies=2), ram)
        self.assertEqual(ram.granted, ["Malphas"])

    async def test_a_reloaded_save_gets_back_only_the_souls_it_lacks(self):
        # The save was made after the first copy arrived, and two more have arrived since.
        ram = FakeRam(owned={"Flying Armor"})
        await self.ticks(self.ctx(received_copies=3), ram)
        self.assertEqual(ram.granted, ["Malphas", "Giant Bat"])

    async def test_nothing_is_granted_with_the_option_off(self):
        ram = FakeRam()
        await self.ticks(self.ctx(received_copies=3, enabled=False), ram)
        self.assertEqual(ram.granted, [])


class ReceivedCopyGrantTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_received_copy_grants_nothing_and_later_items_still_arrive(self):
        # A copy's soul is granted from the client's count of copies, not when the copy is
        # received. Receiving one must still advance the saved received-counter past it, or every
        # later item would wait forever.
        from NetUtils import NetworkItem

        from ..ram import AoSRAM
        from ..ram.addresses import EWRAM
        from .test_deathlink import _new_client
        from .test_ram_map import FakeBackend

        ram = AoSRAM(FakeBackend({EWRAM: bytearray(0x40000), "ROM": bytearray(0x800000)}))
        ctx = SimpleNamespace(items_received=[NetworkItem(PROGRESSIVE_MOVEMENT_CODE, 0, 2),
                                              NetworkItem(item_table["Mind Up (A)"].code, 0, 2)],
                              slot_data={})
        await _new_client()._receive_items(ctx, ram)
        self.assertEqual(await ram.get_received_count(), 2)
        self.assertEqual(await ram.owned_count("consumable", 5), 1)
        self.assertEqual(await ram.owned_count("blue_soul", 0), 0, "a copy grants no soul itself")


if __name__ == "__main__":
    unittest.main()
