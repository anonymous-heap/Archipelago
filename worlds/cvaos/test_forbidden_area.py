"""Generation + logic tests for the Forbidden Area Button option (off / pickup)."""
from BaseClasses import CollectionState, ItemClassification
from test.bases import WorldTestBase

FORBIDDEN_AREA_SWITCH = "Forbidden Area Switch"
_FWD = "A01:20D -> A01:A02"   # the within-A01 forward (20D->A02) connection name prefix


class _Base(WorldTestBase):
    game = "Castlevania - Aria of Sorrow"


class TestForbiddenAreaOff(_Base):
    options = {"forbidden_area_button": "off"}

    def test_no_key_in_pool(self):
        self.assertNotIn(FORBIDDEN_AREA_SWITCH, [i.name for i in self.multiworld.itempool])

    def test_forward_blocked(self):
        ent = next(e for e in self.multiworld.get_entrances()
                   if e.player == self.player and e.name.startswith(_FWD))
        # off keeps the vanilla CSV rule (Impossible) -> never passable in logic
        self.assertFalse(ent.access_rule(self.multiworld.get_all_state(False)))


class TestForbiddenAreaPickup(_Base):
    options = {"forbidden_area_button": "pickup"}

    def test_exactly_one_key(self):
        names = [i.name for i in self.multiworld.itempool]
        self.assertEqual(names.count(FORBIDDEN_AREA_SWITCH), 1)

    def test_key_is_progression(self):
        key = next(i for i in self.multiworld.itempool if i.name == FORBIDDEN_AREA_SWITCH)
        self.assertEqual(key.classification, ItemClassification.progression)

    def test_pool_not_overfilled(self):
        # The Key displaces a filler (it doesn't grow the pool), so the pool still fits the locations.
        self.assertLessEqual(len(self.multiworld.itempool),
                             len(self.multiworld.get_locations(self.player)))

    def test_forward_gated_on_key(self):
        ent = next(e for e in self.multiworld.get_entrances()
                   if e.player == self.player and e.name.startswith(_FWD))
        st = CollectionState(self.multiworld)
        self.assertFalse(ent.access_rule(st), "forward should be blocked without the Key")
        st.collect(self.world.create_item(FORBIDDEN_AREA_SWITCH), prevent_sweep=True)
        self.assertTrue(ent.access_rule(st), "forward should open with the Key")


# --- Direct create_itempool checks: the Key displaces exactly one filler (net-zero pool size) ---
import types
from BaseClasses import ItemClassification as _IC
from .items import create_itempool as _create_itempool
from .options import ForbiddenAreaButton as _FAB


def _mock_world(fab_value):
    return types.SimpleNamespace(
        player=1,
        options=types.SimpleNamespace(
            hard_mode=0,
            forbidden_area_button=types.SimpleNamespace(value=fab_value),
        ),
    )


def test_key_displaces_one_filler():
    off = _create_itempool(_mock_world(_FAB.option_off))
    pick = _create_itempool(_mock_world(_FAB.option_pickup))
    assert len(off) == len(pick), "Key should displace a filler, not grow the pool"
    assert FORBIDDEN_AREA_SWITCH not in [i.name for i in off]
    assert [i.name for i in pick].count(FORBIDDEN_AREA_SWITCH) == 1
    n_filler = lambda pool: sum(i.classification == _IC.filler for i in pool)
    assert n_filler(pick) == n_filler(off) - 1, "exactly one filler should be displaced"


# --- ROM: pickup mode replaces the A01 press-button with an inert candle (in place) ---
def test_rom_button_becomes_inert_candle():
    import os
    from .rom import forbidden_area_button as fab
    rom_path = os.environ.get("CVAOS_USA_ROM",
        "/mnt/c/Users/belmo/Downloads/Castlevania - Aria of Sorrow (USA)/Castlevania - Aria of Sorrow (USA).gba")
    if not os.path.exists(rom_path):
        import pytest; pytest.skip("USA ROM not available")
    base = open(rom_path, "rb").read()
    off = fab.A01_BUTTON_ENTITY_GBA - 0x08000000
    # before: the press-button (object-0x35, varB=48)
    assert base[off + 5] == 0x02 and base[off + 6] == 0x35
    assert int.from_bytes(base[off + 10:off + 12], "little") == 48
    w = fab.build_writes(base)
    assert set(w) == {off} and len(w[off]) == 12, "should be a single in-place 12-byte write"
    b = bytearray(base); b[off:off + 12] = w[off]
    # after: an inert candle (subtype 0x07, varA=varB=0); barrier (next entity) untouched
    assert b[off + 5] == 0x02 and b[off + 6] == 0x07
    assert int.from_bytes(b[off + 8:off + 10], "little") == 0
    assert int.from_bytes(b[off + 10:off + 12], "little") == 0
    assert bytes(b[off + 12:off + 24]) == base[off + 12:off + 24], "barrier must be untouched"
