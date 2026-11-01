"""
Universal Tracker's Find Route: the route this world describes for a location.

    python -m pytest worlds/cvaos/test/test_tracker_route.py -v
"""
from __future__ import annotations

import unittest

from .. import CVAOSWorld
from ..regions import UT_TRICKS_ITEM
from ..tracker_route import DOOR_POINTS, describe_route, plan_route
from .test_universal_tracker import generate, reachable, regenerate_from, state_with

TERRACE = "Terrace [503 CASTLE MAP 3]"


def lines(messages) -> list[str]:
    return ["".join(part["text"] for part in message) for message in messages]


class DescribeRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.multiworld = generate({})

    def route(self, dest_name: str, items: list[str], multiworld=None):
        multiworld = multiworld or self.multiworld
        return describe_route(multiworld.worlds[1], dest_name, state_with(multiworld, items))

    def test_a_route_lists_rooms_by_area_and_names_what_they_need(self):
        route = lines(self.route(TERRACE, ["Malphas", "Hippogryph"]))
        self.assertEqual([line.split(":")[0] for line in route[:-1]],
                         ["Castle Corridor", "Floating Garden"])
        self.assertTrue(route[0].startswith("Castle Corridor: 000 > "), "the route starts at 000")
        self.assertEqual(route[-1], "-> " + TERRACE)
        text = "\n".join(route)
        self.assertIn("(Hippogryph)", text)
        self.assertIn("(Malphas)", text)

    def test_part_of_a_name_finds_its_location(self):
        route = lines(self.route("lucky charm", ["Malphas"]))
        self.assertEqual(route[-1], "-> Entrance Parapet [000 Lucky Charm]")

    def test_there_is_no_route_out_of_reach_or_to_anything_else(self):
        # UT then tries its state for tricks, and then its own route to a region or entrance.
        self.assertIsNone(self.route(TERRACE, []))
        self.assertIsNone(self.route("Entrance: 000:003", ["Malphas", "Hippogryph"]))
        self.assertIsNone(self.route("no such place", ["Malphas"]))

    def test_a_route_that_needs_a_trick_says_so(self):
        rebuilt = regenerate_from(generate({}).worlds[1].fill_slot_data())
        items = ["Malphas"]
        only_with_tricks = reachable(rebuilt, items + [UT_TRICKS_ITEM]) - reachable(rebuilt, items)
        location = sorted(only_with_tricks)[0]
        route = lines(self.route(location, items + [UT_TRICKS_ITEM], rebuilt))
        self.assertEqual(route[0], "Using tricks:")
        self.assertIn("a trick", "\n".join(route))
        self.assertEqual(route[-1], "-> " + location)

    def test_universal_tracker_asks_the_world_for_its_route(self):
        world = self.multiworld.worlds[1]
        state = state_with(self.multiworld, ["Malphas"])
        self.assertEqual(world.get_logical_path("lucky charm", state),
                         describe_route(world, "lucky charm", state))
        # UT shows this docstring in its command help.
        self.assertTrue(CVAOSWorld.get_logical_path.__doc__)


class PlanRouteTest(unittest.TestCase):
    """The Aria of Sorrow tracker's routes, which start in the room Soma is in."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.multiworld = generate({})

    def test_a_route_starts_at_the_door_soma_is_by(self):
        # With only Malphas, Soma can't cross room 108 from the door to 104 to the door to 109,
        # so the short way through 109's door isn't his way from the 104 door.
        world = self.multiworld.worlds[1]
        state = state_with(self.multiworld, ["Malphas"])
        target = world.get_location("Underneath Grand Staircase [109 Silk Robe]")
        from_anywhere = plan_route(world, target, state, "108")
        self.assertEqual(from_anywhere.entrances[0].parent_region.name, "Entrance: 108:109")
        from_104 = plan_route(world, target, state, "108", DOOR_POINTS["108:104"])
        self.assertEqual(from_104.entrances[0].parent_region.name, "Entrance: 108:104")
        self.assertNotIn("Door: 108:109 -> 109:108", [e.name for e in from_104.entrances])
        self.assertEqual(from_104.start_room, "108")


if __name__ == "__main__":
    unittest.main()
