"""
Tests for seed depth (seed_depth.py): the share of a player's pickup locations that the seed's
sphere order reaches before a location, a room or an enemy.

    python -m pytest worlds/cvaos/test/test_seed_depth.py -v
"""
from __future__ import annotations

import threading
import unittest
from unittest import mock

from .. import seed_depth
from ..data import enemy_type_by_name, enemy_type_collection
from ..enemy_checks import defeat_location_name, enemy_id_for_location, placements_of
from ..locations import pickup_location_ids
from .test_smoothing_location_settings import generate


class SeedDepthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.multiworld = generate(3, {"enemy_checks": "all"})
        cls.world = cls.multiworld.worlds[1]
        cls.depths = seed_depth.seed_depths(cls.world)

    def test_every_depth_is_between_zero_and_one(self):
        for table in (self.depths.locations, self.depths.rooms, self.depths.enemies):
            self.assertTrue(table)
            self.assertTrue(all(0.0 <= depth <= 1.0 for depth in table.values()))

    def test_pickups_follow_the_sphere_order(self):
        sphere_of = {location.name: index
                     for index, sphere in enumerate(self.multiworld.get_spheres())
                     for location in sphere if location.player == 1}
        pickups = sorted((sphere_of[location.name], self.depths.locations[location.name])
                         for location in self.multiworld.get_locations(1)
                         if location.address in pickup_location_ids)
        self.assertEqual(pickups[0][1], 0.0)
        depths = [depth for _sphere, depth in pickups]
        self.assertEqual(depths, sorted(depths))

    def test_an_enemy_is_as_deep_as_the_first_room_where_it_appears(self):
        for enemy in enemy_type_collection:
            rooms = [self.depths.rooms[placement.room] for placement in placements_of(enemy.name)
                     if placement.room in self.depths.rooms]
            with self.subTest(enemy=enemy.name):
                self.assertEqual(self.depths.enemies[enemy.id], min(rooms, default=1.0))

    def test_a_defeat_location_takes_its_enemys_depth(self):
        defeats = [location for location in self.multiworld.get_locations(1)
                   if location.address not in pickup_location_ids]
        self.assertEqual(len(defeats), 112)
        for location in defeats:
            enemy_id = enemy_id_for_location(location.address)
            self.assertEqual(self.depths.locations[location.name], self.depths.enemies[enemy_id])

    def test_an_enemy_the_sweep_never_reaches_counts_as_one(self):
        graham_rooms = {placement.room for placement in placements_of("Graham")}
        real = seed_depth.can_reach_room

        def unreachable_graham(state, world, room):
            return room not in graham_rooms and real(state, world, room)

        with mock.patch.object(seed_depth, "can_reach_room", unreachable_graham):
            depths = seed_depth._sweep(self.world)
        self.assertEqual(depths.enemies[enemy_type_by_name["Graham"].id], 1.0)
        self.assertEqual(depths.locations[defeat_location_name("Graham")], 1.0)

    def test_output_threads_share_one_sweep(self):
        world = generate(4, {}).worlds[1]
        results = []
        with mock.patch.object(seed_depth, "_sweep", wraps=seed_depth._sweep) as sweep:
            threads = [threading.Thread(
                target=lambda: results.append(seed_depth.seed_depths(world))) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        sweep.assert_called_once_with(world)
        self.assertEqual(len(results), 4)
        self.assertTrue(all(result is results[0] for result in results))
        self.assertIs(seed_depth.enemy_seed_depths(world), results[0].enemies)


if __name__ == "__main__":
    unittest.main()
