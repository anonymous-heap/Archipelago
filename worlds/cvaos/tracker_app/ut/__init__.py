"""
Universal Tracker's logic engine, which the Aria of Sorrow tracker runs its logic with.

``TrackerCore.py`` is Universal Tracker's own file, copied unchanged so that an update from
upstream is a matter of copying it again (see :data:`UPSTREAM`). It rebuilds this world from the
seed's slot data and reports which locations are in logic, and it never touches a UI.

The engine imports a few names from UT's package, which also registers a world and a Launcher
entry of its own. Those would clash with an installed copy of UT, so this module defines just the
names the engine needs. Each matches UT's definition at the version in :data:`UPSTREAM`.
"""
from __future__ import annotations

from collections import Counter
from enum import Enum
from typing import Any, NamedTuple, Optional

from BaseClasses import CollectionState, Entrance

# Where TrackerCore.py comes from. test/test_tracker_app.py checks the file against sha256, taken
# with LF line endings as git stores it upstream.
UPSTREAM = {
    "repository": "https://github.com/FarisTheAncient/Archipelago",
    "path": "worlds/tracker/TrackerCore.py",
    "version": "Tracker_v0.3.8",
    "commit": "38e4c00126d06141ccc0cd4ba64ea0c0616c5e3f",
    "sha256": "5391853ab58c3032580278e505128cade010c63ac164c8f74b212682c01ababa",
}

UT_VERSION = "v0.3.8"


class CurrentTrackerState(NamedTuple):
    all_items: Counter
    prog_items: Counter
    glitched_locations: list[str]
    events: list[str]
    event_locations: list[str]
    in_logic_locations: list[str]
    in_logic_regions: list[str]
    unconnected_entrances: list[Entrance]
    readable_locations: list[str]
    hinted_locations: list
    state: Optional[CollectionState]
    glitches_state: Optional[CollectionState]

    @staticmethod
    def init_empty_state() -> "CurrentTrackerState":
        return CurrentTrackerState(Counter(), Counter(), [], [], [], [], [], [], [], [], None, None)


class DeferredEntranceMode(Enum):
    forced = "on"
    default = "default"
    disabled = "off"


class TrackerException(Exception):
    def __init__(self, *args: object, message: str) -> None:
        self.message = message
        super().__init__(*args)


class HostImportException(Exception):
    pass


class TrackerWorld:
    """Stands in for UT's TrackerWorld, whose only use to the engine is its host.yaml settings.

    These are UT's defaults, except that nothing is saved between sessions.
    """
    settings: dict[str, Any] = {
        "player_files_path": "",
        "include_region_name": False,
        "include_location_name": True,
        "hide_excluded_locations": False,
        "use_split_map_icons": True,
        "enforce_deferred_entrances": "default",
        "display_glitched_logic": True,
        "save_entered_commands": False,
        "sorting_priorities": {},
        "sorting_method": "apworld",
    }


class UTMapTabData:
    """UT's map tab settings for a world. The engine imports the name but never uses it here."""
