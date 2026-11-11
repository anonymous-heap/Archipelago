"""Generation + logic tests for the Forbidden Area Button option (off / pickup)."""
import unittest
from collections import Counter

from BaseClasses import CollectionState, ItemClassification
from test.bases import WorldTestBase
from test.general import gen_steps, setup_multiworld

from .. import CVAOSWorld
from ..items import create_itempool
from . import find_rom

FORBIDDEN_AREA_SWITCH = "Study Sealswitch"
_FWD = "A01:20D -> A01:A02"   # the within-A01 forward (20D->A02) connection name prefix
_REV = "A01:A02 -> A01:20D"   # the within-A01 reverse (A02->20D), up from the Forbidden Area


class TestForbiddenAreaOff(WorldTestBase):
    # A WorldTestBase, so its built-in tests also fill a seed with the button off.
    game = "Castlevania - Aria of Sorrow"
    options = {"forbidden_area_button": "off"}

    def test_forward_blocked(self):
        ent = next(e for e in self.multiworld.get_entrances()
                   if e.player == self.player and e.name.startswith(_FWD))
        # off keeps the vanilla CSV rule (Impossible) -> never passable in logic
        self.assertFalse(ent.access_rule(self.multiworld.get_all_state(False)))

    def test_reverse_needs_only_a_way_up(self):
        # The button beside the A02 hole opens the barrier, so climbing out is enough.
        ent = next(e for e in self.multiworld.get_entrances()
                   if e.player == self.player and e.name.startswith(_REV))
        st = CollectionState(self.multiworld)
        st.collect(self.world.create_item("Hippogryph"), prevent_sweep=True)
        self.assertTrue(ent.access_rule(st))


class ForbiddenAreaPickupTest(unittest.TestCase):
    """pickup is the default setting, so the core tests already fill a seed with it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.multiworld = setup_multiworld(CVAOSWorld, gen_steps, seed=1,
                                          options={"forbidden_area_button": "pickup"})
        cls.world = cls.multiworld.worlds[1]

    def entrance(self, prefix: str):
        return next(e for e in self.multiworld.get_entrances()
                    if e.player == 1 and e.name.startswith(prefix))

    def test_key_is_progression(self):
        key = next(i for i in self.multiworld.itempool if i.name == FORBIDDEN_AREA_SWITCH)
        self.assertEqual(key.classification, ItemClassification.progression)

    def test_forward_gated_on_key(self):
        ent = self.entrance(_FWD)
        st = CollectionState(self.multiworld)
        self.assertFalse(ent.access_rule(st), "forward should be blocked without the Key")
        st.collect(self.world.create_item(FORBIDDEN_AREA_SWITCH), prevent_sweep=True)
        self.assertTrue(ent.access_rule(st), "forward should open with the Key")

    def test_reverse_needs_the_key_and_a_way_up(self):
        # Without the button, the barrier blocks the way up from the Forbidden Area as well.
        ent = self.entrance(_REV)
        st = CollectionState(self.multiworld)
        st.collect(self.world.create_item("Hippogryph"), prevent_sweep=True)
        self.assertFalse(ent.access_rule(st), "a way up is not enough without the Key")
        st.collect(self.world.create_item(FORBIDDEN_AREA_SWITCH), prevent_sweep=True)
        self.assertTrue(ent.access_rule(st))
        key_only = CollectionState(self.multiworld)
        key_only.collect(self.world.create_item(FORBIDDEN_AREA_SWITCH), prevent_sweep=True)
        self.assertFalse(ent.access_rule(key_only), "the Key is not enough without a way up")


def _pool(setting: str) -> Counter:
    world = setup_multiworld(CVAOSWorld, ("generate_early",), seed=1,
                             options={"forbidden_area_button": setting}).worlds[1]
    return Counter(item.name for item in create_itempool(world))


def test_the_key_takes_the_place_of_one_100_gold():
    # The Key displaces the smallest gold pickup rather than a unique filler item such as the
    # game's only Lucky Charm. One item replaces another, so the pool size still equals the
    # location count.
    off = _pool("off")
    pickup = _pool("pickup")
    assert off[FORBIDDEN_AREA_SWITCH] == 0
    assert pickup - off == Counter({FORBIDDEN_AREA_SWITCH: 1})
    assert off - pickup == Counter({"100 Gold": 1})


# --- ROM: pickup mode replaces the A01 press-button with an inert object (in place) ---
def test_rom_button_becomes_inert_object():
    from ..rom import forbidden_area_button as fab
    rom_path = find_rom()
    if rom_path is None:
        import pytest; pytest.skip("USA ROM not available")
    base = open(rom_path, "rb").read()
    off = fab.A01_BUTTON_ENTITY_GBA - 0x08000000
    # before: the press-button (object-0x35, varB=48)
    assert base[off + 5] == 0x02 and base[off + 6] == 0x35
    assert int.from_bytes(base[off + 10:off + 12], "little") == 48
    w = fab.build_writes(base)
    assert set(w) == {off} and len(w[off]) == 12, "should be a single in-place 12-byte write"
    b = bytearray(base); b[off:off + 12] = w[off]
    # after: object 07 with varA=4 and varB=0, which does nothing. The barrier (next entity) is
    # untouched. varA=0 would not be inert, because it is Castle Corridor's particle effect.
    assert b[off + 5] == 0x02 and b[off + 6] == 0x07
    assert int.from_bytes(b[off + 8:off + 10], "little") == 4
    assert int.from_bytes(b[off + 10:off + 12], "little") == 0
    assert bytes(b[off + 12:off + 24]) == base[off + 12:off + 24], "barrier must be untouched"
