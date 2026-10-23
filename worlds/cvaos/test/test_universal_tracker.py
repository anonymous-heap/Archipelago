"""
Tests that rebuilding the world from slot_data alone reproduces the original generation's logic.

Universal Tracker passes slot_data through the static ``interpret_slot_data`` and stores the
result in ``multiworld.re_gen_passthrough[game]``. It then runs the generation steps up to
``generate_basic`` with default options. These tests do the same and compare the rebuilt world
with the original.

    python -m pytest worlds/cvaos/test/test_universal_tracker.py -v
"""
from __future__ import annotations

import inspect
import unittest
from collections import Counter

from BaseClasses import CollectionState, MultiWorld
from test.general import setup_multiworld
from worlds.AutoWorld import call_all

from .. import CVAOSWorld
from ..options import LOGIC_OPTION_NAMES, resolve_allowed_techniques

UT_STEPS = ("generate_early", "create_regions", "create_items", "set_rules", "connect_entrances",
            "generate_basic")

# Options that create_items reads to decide which items item smoothing places itself in pre_fill.
# They move items between the multiworld pool and the pre-fill items, and never change what logic
# needs, so slot_data does not carry them.
READ_BUT_NOT_LOGIC = frozenset({"item_smoothing", "early_mobility_souls", "black_panther_bias",
                                "non_local_items", "plando_items", "priority_locations",
                                "exclude_locations"})

# Pairs of (probed option, configuration). Each configuration is checked to change the logic when
# its probed option goes back to the default, so slot_data failing to carry that option makes the
# rebuilt world's logic differ from the original's.
ROUND_TRIPS = (
    ("goal", {"goal": "graham"}),
    ("hard_mode", {"hard_mode": True}),
    ("hard_mode_pickups", {"hard_mode_pickups": "always"}),
    ("hard_mode_pickups", {"hard_mode": True, "hard_mode_pickups": "never"}),
    ("progressive_movement", {"progressive_movement": True}),
    ("forbidden_area_button", {"forbidden_area_button": "off"}),
    ("oops_all_whips", {"oops_all_whips": True}),
    ("logic_difficulty_preset", {"logic_difficulty_preset": "standard"}),
    ("logic_difficulty_preset", {"logic_difficulty_preset": "expert"}),
    ("kickable_enemy_logic", {"kickable_enemy_logic": "in_logic"}),
    ("kickable_enemy_logic", {"logic_difficulty_preset": "expert",
                              "kickable_enemy_logic": "out_of_logic"}),
    ("platform_clip_logic", {"platform_clip_logic": "in_logic"}),
    ("pixel_perfect_logic", {"pixel_perfect_logic": "in_logic"}),
    ("quick_save_reset_logic", {"quick_save_reset_logic": "in_logic"}),
    ("inbounds_zip_logic", {"inbounds_zip_logic": "in_logic"}),
)


def generate(options: dict) -> MultiWorld:
    return setup_multiworld(CVAOSWorld, UT_STEPS, seed=1, options=options)


def regenerate_from(slot_data: dict) -> MultiWorld:
    """Rebuild the world as Universal Tracker does: default options, plus interpret_slot_data's
    result in multiworld.re_gen_passthrough."""
    multiworld = setup_multiworld(CVAOSWorld, (), seed=2)
    multiworld.re_gen_passthrough = {CVAOSWorld.game: CVAOSWorld.interpret_slot_data(slot_data)}
    for step in UT_STEPS:
        call_all(multiworld, step)
    return multiworld


def state_with(multiworld: MultiWorld, item_names: list[str]) -> CollectionState:
    state = CollectionState(multiworld)
    for name in item_names:
        state.collect(multiworld.worlds[1].create_item(name), True)
    state.sweep_for_advancements()
    return state


def reachable(multiworld: MultiWorld, item_names: list[str]) -> frozenset[str]:
    state = state_with(multiworld, item_names)
    return frozenset(loc.name for loc in multiworld.get_locations(1) if loc.can_reach(state))


def logic_fingerprint(multiworld: MultiWorld) -> tuple:
    """Everything the tracker's logic depends on, in a comparable form: the resolved techniques,
    the locations, the item pool, what each progression item reaches on its own, what the other
    progression items reach without it, and whether growing prefixes of the progression items
    reach the goal."""
    world = multiworld.worlds[1]
    items = [*multiworld.itempool, *world.get_pre_fill_items()]
    progression = sorted({item.name for item in items if item.advancement})
    prefixes = [progression[:n] for n in range(0, len(progression) + 1, 4)]
    return (
        resolve_allowed_techniques(world.options),
        frozenset(loc.name for loc in multiworld.get_locations(1)),
        frozenset(Counter(item.name for item in items).items()),
        tuple(reachable(multiworld, [name]) for name in progression),
        tuple(reachable(multiworld, [other for other in progression if other != name])
              for name in progression),
        tuple(multiworld.can_beat_game(state_with(multiworld, prefix)) for prefix in prefixes),
    )


class InterfaceTest(unittest.TestCase):
    def test_interpret_slot_data_is_callable_on_the_class(self):
        # Universal Tracker calls interpret_slot_data on the class, before any world exists, and
        # skips the YAML only for worlds that set ut_can_gen_without_yaml.
        method = inspect.getattr_static(CVAOSWorld, "interpret_slot_data")
        self.assertIsInstance(method, (staticmethod, classmethod))
        self.assertTrue(CVAOSWorld.ut_can_gen_without_yaml)

    def test_every_option_read_while_building_logic_is_listed(self):
        multiworld = setup_multiworld(CVAOSWorld, (), seed=1)
        world = multiworld.worlds[1]
        options = world.options
        read: set[str] = set()

        class Recorder:
            """Stands in for the world's options and records which ones are read."""

            def __getattr__(self, name):
                read.add(name)
                return getattr(options, name)

            def __setattr__(self, name, value):
                setattr(options, name, value)

        world.options = Recorder()
        for step in UT_STEPS:
            call_all(multiworld, step)
        self.assertTrue(read)
        self.assertLessEqual(read - READ_BUT_NOT_LOGIC, set(LOGIC_OPTION_NAMES))

    def test_every_listed_option_is_probed(self):
        self.assertEqual({probed for probed, _config in ROUND_TRIPS}, set(LOGIC_OPTION_NAMES))


class RoundTripTest(unittest.TestCase):
    def test_each_logic_option_survives_the_rebuild(self):
        for probed, config in ROUND_TRIPS:
            with self.subTest(probed=probed, **config):
                original = generate(config)
                rebuilt = regenerate_from(original.worlds[1].fill_slot_data())
                for name in LOGIC_OPTION_NAMES:
                    self.assertEqual(getattr(rebuilt.worlds[1].options, name).value,
                                     getattr(original.worlds[1].options, name).value, name)
                fingerprint = logic_fingerprint(original)
                without_probe = {name: value for name, value in config.items() if name != probed}
                self.assertNotEqual(logic_fingerprint(generate(without_probe)), fingerprint,
                                    f"{probed} must change the logic in this configuration")
                self.assertEqual(logic_fingerprint(rebuilt), fingerprint)

    def test_slot_data_from_an_older_seed_keeps_defaults_and_warns(self):
        options = {"goal": "graham", "pixel_perfect_logic": "in_logic"}
        slot_data = generate(options).worlds[1].fill_slot_data()
        older = {name: value for name, value in slot_data.items() if name != "pixel_perfect_logic"}
        with self.assertLogs("Client", level="WARNING") as logs:
            rebuilt = regenerate_from(older).worlds[1].options
        self.assertIn("pixel_perfect_logic", logs.output[0])
        self.assertEqual(rebuilt.pixel_perfect_logic.value, rebuilt.pixel_perfect_logic.default)
        self.assertEqual(rebuilt.goal.value, slot_data["goal"])

    def test_current_slot_data_logs_no_warning(self):
        slot_data = generate({}).worlds[1].fill_slot_data()
        with self.assertNoLogs("Client", level="WARNING"):
            CVAOSWorld.interpret_slot_data(slot_data)


if __name__ == "__main__":
    unittest.main()
