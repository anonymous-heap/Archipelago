"""
Universal Tracker's map tab: the map data in tracker/ matches this world, and loads the way UT
loads it.

    python -m pytest worlds/cvaos/test/test_tracker_map.py -v
"""
from __future__ import annotations

import json
import pkgutil
import unittest
from collections import Counter

from .. import CVAOSSettings, CVAOSWorld, tracker_map
from ..locations import location_name_to_id


def ut_load(name: str):
    """Read a file in tracker/ as UT does: through pkgutil, relative to the World class's module,
    then as strict JSON."""
    path = f"/{CVAOSWorld.tracker_world['map_page_folder']}/{name}"
    return json.loads(pkgutil.get_data(CVAOSWorld.__module__, path).decode("utf-8-sig"))


class TrackerMapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.maps = ut_load(tracker_map.MAPS_FILE)
        self.locations = ut_load(tracker_map.LOCATIONS_FILE)

    def test_the_shipped_files_match_the_world_data(self):
        maps, locations = tracker_map.build()
        self.assertEqual(self.maps, maps)
        self.assertEqual(self.locations, locations,
                         "rewrite them with: python -m worlds.cvaos.tracker_map")

    def test_every_location_is_a_section_once(self):
        # UT matches a section to a location by its name alone.
        names = [section["name"] for node in self.locations for section in node["sections"]]
        self.assertEqual(sorted(names), sorted(location_name_to_id))

    def test_every_pin_is_on_a_known_map_and_inside_its_image(self):
        sizes = {m.name: m.size for m in tracker_map.MAPS}
        self.assertEqual([m["name"] for m in self.maps], list(sizes))
        for node in self.locations:
            for pin in node["map_locations"]:
                with self.subTest(location=node["sections"][0]["name"], map=pin["map"]):
                    width, height = sizes[pin["map"]]
                    self.assertTrue(0 <= pin["x"] < width and 0 <= pin["y"] < height)

    def test_no_two_pins_share_a_spot(self):
        # UT keys a map's pins by their position, so a second pin there would replace the first.
        spots = Counter((pin["map"], pin["x"], pin["y"])
                        for node in self.locations for pin in node["map_locations"])
        self.assertEqual([spot for spot, count in spots.items() if count > 1], [])

    def test_a_pin_lands_where_its_pickup_is(self):
        # Entrance Parapet's pickup is 384 by 184 pixels into room 000, at map cell (0, 19).
        node = next(node for node in self.locations
                    if node["sections"][0]["name"] == "Entrance Parapet [000 Lucky Charm]")
        castle = next(pin for pin in node["map_locations"] if pin["map"] == "Dracula's Castle")
        self.assertEqual((castle["x"], castle["y"]),
                         (86 + round(24 * (0 + 384 / 256)), 49 + round(24 * (19 + 184 / 256))))

    def test_the_pack_path_is_an_optional_setting(self):
        key = CVAOSWorld.tracker_world["external_pack_key"]
        self.assertIn(key, CVAOSSettings.__annotations__)
        self.assertFalse(CVAOSSettings.UTPoptrackerPath.required)


if __name__ == "__main__":
    unittest.main()
