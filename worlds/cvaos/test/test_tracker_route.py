"""
Routes to a location: the route this world describes for Universal Tracker's Find Route, and the
routes the Aria of Sorrow tracker plans from the room Soma is in.

    python -m pytest worlds/cvaos/test/test_tracker_route.py -v
"""
from __future__ import annotations

import functools
import unittest

from BaseClasses import Entrance, MultiWorld

from .. import CVAOSWorld
from ..regions import UT_TRICKS_ITEM
from ..tracker_route import DOOR_POINTS, describe_route, plan_route, route_steps
from .test_universal_tracker import generate, reachable, regenerate_from, state_with

TERRACE = "Terrace [503 CASTLE MAP 3]"


def lines(messages) -> list[str]:
    return ["".join(part["text"] for part in message) for message in messages]


@functools.cache
def default_multiworld() -> MultiWorld:
    """Return a world generated with the default options, shared by every test in this file."""
    return generate({})


class DescribeRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.multiworld = default_multiworld()

    def route(self, dest_name: str, items: list[str], multiworld=None):
        """Ask the world for a route, as Universal Tracker's Find Route does."""
        multiworld = multiworld or self.multiworld
        return multiworld.worlds[1].get_logical_path(dest_name, state_with(multiworld, items))

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
        # When there is no route, UT tries again with its state for tricks, and then shows its own
        # route to a region or entrance.
        self.assertIsNone(self.route(TERRACE, []))
        self.assertIsNone(self.route("Entrance: 000:003", ["Malphas", "Hippogryph"]))
        self.assertIsNone(self.route("no such place", ["Malphas"]))

    def test_a_route_that_needs_a_trick_says_so(self):
        rebuilt = regenerate_from(self.multiworld.worlds[1].fill_slot_data())
        items = ["Malphas"]
        only_with_tricks = reachable(rebuilt, items + [UT_TRICKS_ITEM]) - reachable(rebuilt, items)
        location = sorted(only_with_tricks)[0]
        route = lines(self.route(location, items + [UT_TRICKS_ITEM], rebuilt))
        self.assertEqual(route[0], "Using tricks:")
        self.assertIn("a trick", "\n".join(route))
        self.assertEqual(route[-1], "-> " + location)

    def test_the_route_command_has_help(self):
        # UT shows this docstring in its command help.
        self.assertTrue(CVAOSWorld.get_logical_path.__doc__)


class GateRouteTest(unittest.TestCase):
    """Routes through the gates whose rules are written in code rather than the routing CSVs."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.multiworld = default_multiworld()
        cls.world = cls.multiworld.worlds[1]
        cls.state = cls.multiworld.get_all_state()

    def needs_in(self, room: str, location_name: str, start_room: str) -> list[str]:
        """Return what a route from ``start_room`` to the location names for ``room``."""
        location = self.world.get_location(location_name)
        route = plan_route(self.world, location, self.state, start_room)
        steps = route_steps(self.state, route.entrances)
        return next(needed for step_room, needed in steps if step_room == room)

    def test_a_route_down_from_the_study_names_the_sealswitch(self):
        # Room A01 lies between the Study's room 20D and the Forbidden Area's room A02.
        self.assertEqual(self.needs_in("A01", "Treasure Chamber [A06 Joyeuse]", "20D"),
                         ["Study Sealswitch"])

    def test_a_route_up_from_the_forbidden_area_names_the_sealswitch_and_a_way_up(self):
        self.assertEqual(self.needs_in("A01", "Behind Tunnel [20E Ancient Book 1]", "A02"),
                         ["Study Sealswitch", "Malphas"])

    def test_a_route_into_the_chaotic_realm_names_what_the_true_ending_needs(self):
        # Every route into the Chaotic Realm crosses the door from 507 to 506, and every route
        # into room 900 crosses the door from 904. Both doors have the same rule.
        needs = ("(the three Ancient Books, Giant Bat, a Flame Demon soul, a Succubus soul,"
                 " beating Graham)")
        crossings = {"Marble Annex [B18 Super Potion (E)]": ("507", "506"),
                     "Balcony Center Item [900 Black Panther]": ("904", "900")}
        for location_name, (room, next_room) in crossings.items():
            with self.subTest(location_name):
                route = lines(describe_route(self.world, location_name, self.state))
                self.assertIn(f"{room} {needs} > {next_room}", "\n".join(route))

    def test_every_rule_a_route_can_cross_names_what_it_needs(self):
        # An entrance without a rule needs nothing, and no route crosses a rule that never passes.
        unnamed = [entrance.name for entrance in self.multiworld.get_entrances(1)
                   if entrance.access_rule is not Entrance.access_rule
                   and entrance.access_rule(self.state)
                   and not hasattr(entrance.access_rule, "uses")]
        self.assertEqual(unnamed, [])


class PlanRouteTest(unittest.TestCase):
    """The Aria of Sorrow tracker's routes, which start at the door Soma came into his room by."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.multiworld = default_multiworld()

    def test_a_route_starts_at_the_door_soma_is_by(self):
        # With only Malphas, Soma can't cross room 108 from the door to 104 to the door to 109.
        # A route from anywhere in 108 can leave by the 109 door, but one from the 104 door can't.
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
