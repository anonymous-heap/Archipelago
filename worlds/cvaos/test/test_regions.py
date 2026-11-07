"""
Tests for the location names and the routing data that the regions are built from.

Run from the Archipelago root directory:
    python -m pytest worlds/cvaos/test/test_regions.py -v
"""
from __future__ import annotations

from ..data import (
    entrance_info_collection,
    pickup_info_collection,
    entrance_to_entrance_info_collection,
)
from ..locations import defeat_location_names, location_name_to_id


def test_location_names_use_friendly_format():
    """Pickup location names are the friendly form '<area> [<room> <item>]', not the item name."""
    # The data-package location names are exactly PickupInfo.location_name for every pickup, plus
    # the "Defeat <enemy>" names of the defeat locations, with no name shared between them.
    assert set(location_name_to_id) == ({p.location_name for p in pickup_info_collection}
                                        | defeat_location_names)
    assert len(location_name_to_id) == len(pickup_info_collection) + len(defeat_location_names)
    # A known pickup renders in the documented format.
    assert "White Dragon Hallway [00D Potion (A)]" in location_name_to_id
    # The bare item name is NOT a location name (items and locations are distinct).
    assert "Potion" not in location_name_to_id


def test_routing_data_loaded():
    """Test that routing data was loaded."""
    assert len(entrance_to_entrance_info_collection) > 0, "No routing info was loaded"


def test_starting_entrance_exists():
    """Test that the starting entrance (000:003) exists."""
    entrance_ids = {e.door_identifier_unique for e in entrance_info_collection}
    assert "000:003" in entrance_ids, "Starting entrance 000:003 not found"
