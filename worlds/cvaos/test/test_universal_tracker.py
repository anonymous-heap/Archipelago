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
from Options import Choice, Range, Toggle
from test.general import setup_multiworld
from worlds.AutoWorld import call_all

from .. import CVAOSWorld
from ..options import LOGIC_OPTION_NAMES, resolve_allowed_techniques
from ..regions import UT_TRICKS_ITEM

UT_STEPS = ("generate_early", "create_regions", "create_items", "set_rules", "connect_entrances",
            "generate_basic")

# Pairs of (probed option, configuration). Each configuration is checked to change the logic when
# its probed option goes back to the default, so slot_data failing to carry that option makes the
# rebuilt world's logic differ from the original's.
ROUND_TRIPS = (
    ("goal", {"goal": "graham"}),
    ("hard_mode", {"hard_mode": True}),
    ("forbidden_area_button", {"forbidden_area_button": "off"}),
    ("logic_difficulty_preset", {"logic_difficulty_preset": "standard"}),
    ("logic_difficulty_preset", {"logic_difficulty_preset": "expert"}),
    ("kickable_enemy_logic", {"kickable_enemy_logic": "in_logic"}),
    ("kickable_enemy_logic", {"logic_difficulty_preset": "expert",
                              "kickable_enemy_logic": "out_of_logic"}),
    ("platform_clip_logic", {"platform_clip_logic": "in_logic"}),
    ("pixel_perfect_logic", {"pixel_perfect_logic": "in_logic"}),
    ("quick_save_reset_logic", {"quick_save_reset_logic": "in_logic"}),
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
    the locations, the item pool, what each progression item reaches on its own, and whether
    growing prefixes of the progression items reach the goal."""
    world = multiworld.worlds[1]
    progression = sorted({item.name for item in multiworld.itempool if item.advancement})
    prefixes = [progression[:n] for n in range(0, len(progression) + 1, 4)]
    return (
        resolve_allowed_techniques(world.options),
        frozenset(loc.name for loc in multiworld.get_locations(1)),
        frozenset(Counter(item.name for item in multiworld.itempool).items()),
        tuple(reachable(multiworld, [name]) for name in progression),
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
        self.assertLessEqual(read, set(LOGIC_OPTION_NAMES))

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

    def test_a_value_this_version_doesn_t_know_keeps_the_default_and_warns(self):
        # A seed generated by a newer version of the world can carry a choice this one lacks.
        slot_data = dict(generate({}).worlds[1].fill_slot_data(), logic_difficulty_preset=99)
        with self.assertLogs("Client", level="WARNING") as logs:
            rebuilt = regenerate_from(slot_data).worlds[1].options
        self.assertIn("logic_difficulty_preset", "\n".join(logs.output))
        self.assertEqual(rebuilt.logic_difficulty_preset.value,
                         rebuilt.logic_difficulty_preset.default)


def unlisted_option_values() -> dict:
    """A value other than its default for each option not in LOGIC_OPTION_NAMES, of the kinds that
    have one: toggles, choices and ranges."""
    values = {}
    for name, option in CVAOSWorld.options_dataclass.type_hints.items():
        if name in LOGIC_OPTION_NAMES:
            continue
        if issubclass(option, Toggle):
            values[name] = int(not option.default)
        elif issubclass(option, Choice):
            values[name] = next(value for value in sorted(set(option.options.values()))
                                if value != option.default)
        elif issubclass(option, Range):
            values[name] = (option.range_start if option.default != option.range_start
                            else option.range_end)
    return values


class UnlistedOptionsTest(unittest.TestCase):
    def test_options_left_out_of_slot_data_leave_the_logic_alone(self):
        # The recorder above misses an option that a rule reads only when it runs, and one read
        # only while another option is away from its default. Moving every unlisted option away
        # from its default, with the logic options at their defaults and then away from them,
        # catches both, since slot_data doesn't carry the unlisted ones.
        unlisted = unlisted_option_values()
        self.assertTrue(unlisted)
        logic_changed = {name: value for _probed, config in ROUND_TRIPS
                         for name, value in config.items()}
        for logic in ({}, logic_changed):
            with self.subTest(logic=logic):
                self.assertEqual(logic_fingerprint(generate({**logic, **unlisted})),
                                 logic_fingerprint(generate(logic)))


class TricksTierTest(unittest.TestCase):
    """UT shows the locations that only out-of-logic tricks reach in its second colour."""

    ITEM_SETS = ([], ["Malphas"], ["Malphas", "Hippogryph"], ["Malphas", "Skula"])

    def gained(self, multiworld: MultiWorld) -> list[frozenset[str]]:
        return [reachable(multiworld, items + [UT_TRICKS_ITEM]) - reachable(multiworld, items)
                for items in self.ITEM_SETS]

    def test_the_tricks_item_opens_trick_routes_while_tracking(self):
        self.assertEqual(CVAOSWorld.glitches_item_name, UT_TRICKS_ITEM)
        rebuilt = regenerate_from(generate({}).worlds[1].fill_slot_data())  # every trick out
        gained = self.gained(rebuilt)
        self.assertTrue(any(gained), "out-of-logic tricks reach no extra location")
        for items in self.ITEM_SETS:
            with self.subTest(items=items):
                self.assertLessEqual(reachable(rebuilt, items),
                                     reachable(rebuilt, items + [UT_TRICKS_ITEM]))

    def test_a_real_generation_has_no_trick_routes(self):
        self.assertFalse(any(self.gained(generate({}))))

    def test_tricks_already_in_logic_add_nothing(self):
        every_trick = {"logic_difficulty_preset": "expert", "quick_save_reset_logic": "in_logic"}
        rebuilt = regenerate_from(generate(every_trick).worlds[1].fill_slot_data())
        self.assertFalse(any(self.gained(rebuilt)))


if __name__ == "__main__":
    unittest.main()
