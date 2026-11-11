"""
Entry states: where Soma is once he has come into a room by a door and has stopped moving.

A question in ``benchmark.py`` starts with Soma just inside a door. ``Session.enter`` puts him at
the door's landing, which is the annotator's model of where a player arrives. The game often
moves him from there before a player could act. A wooden door moves him about 20 px into the
room, a boss's arena shuts its door and pushes him in, and water lifts him unless he has the soul
that sinks. The state that follows is his **entry state**, and it is where a search starts.

:func:`arrive` brings Soma in by a door and presses nothing until he stops moving. It returns an
:class:`Entry`, which says how the arrival ended and where he is.

``entry_states.json`` holds the entry of every door as EmuHawk gave it, and :func:`stored` reads
it. A search doesn't need the file in order to run, because :func:`arrive` measures the same
thing live. The file says ahead of time which arrivals don't end with Soma at rest. It is also a
reference for another emulator, on which every arrival must end as the file says.

An arrival depends on the loadout only through the enchant soul that is equipped, because every
other movement soul does nothing until a button is pressed. So the file has one entry for each
door with no enchant soul, and one more for ``Dive`` or ``WWalk`` where that soul changes how the
arrival ends. Arrivals with six large loadouts were measured at every door as a check, and each
one ended as the file's entry for its enchant soul does.

Every stored entry was measured from the state that follows ``Session.boot``, with the room's
enemies where the game first puts them. An enemy can hit Soma while he arrives, and the entry
then has ``hurt`` set. Where such an arrival ends depends on what the enemies do, so an arrival
from another state of the game can end elsewhere in those rooms.
"""
from __future__ import annotations

import functools
import json
import os
from dataclasses import asdict, dataclass, replace
from typing import Iterable, Optional

from ...data import room_info
from ..annotator import emulator as annotator
from . import game, success

# An arrival has settled when nothing about Soma has changed for this many frames.
SETTLE = 40
# The frames that an arrival is given to settle in.
LIMIT = 420
# The enchant souls that an arrival is measured with: none, and each one that is a movement token.
ENCHANTS = ("", *annotator.YELLOW_SOULS)
OUTCOMES = ("settles", "leaves", "unsettled")
ENTRY_FILE = "entry_states.json"


@dataclass(frozen=True)
class Entry:
    """How one arrival by a door ended, with no button pressed.

    ``outcome`` is one of three words:

    * ``"settles"``: Soma came to rest in the room, and ``x`` and ``y`` are where.
    * ``"leaves"``: Soma left the room again. A top door whose doorway is under water is the
      usual case, because he floats up and out of it unless ``Dive`` is equipped. ``x`` and
      ``y`` are where he was last seen in the room.
    * ``"unsettled"``: Soma was still moving after ``LIMIT`` frames, for example because he
      stands on a platform that moves. ``x`` and ``y`` are where he was then.
    """

    door: str       # the door's unique identifier, such as "009:008"
    # True for an arena that was entered with its boss there (see success.prepare).
    boss: bool
    enchant: str    # the enchant soul that was equipped: "", "Dive" or "WWalk"
    outcome: str
    x: int          # Soma's feet in room pixels, which are 1 px above a floor that he stands on
    y: int
    # The frames after the arrival at which Soma last changed, or at which he left the room.
    # It is LIMIT for an arrival that didn't settle.
    frames: int
    hurt: bool = False               # True when Soma lost HP on the way
    left_by: Optional[str] = None    # the door that he left by, when a door explains his leaving

    @property
    def key(self) -> tuple[str, bool, str]:
        """The door, the arena's state and the enchant soul, which name one arrival."""
        return self.door, self.boss, self.enchant


def equipped_enchant(tokens: Iterable[str]) -> str:
    """Return the enchant soul that ``Session.set_loadout`` equips for a loadout, or ``""``."""
    tokens = set(tokens)
    return next((token for token in annotator.YELLOW_SOULS if token in tokens), "")


def arrive(session: game.Session, door: str, boss: bool = False,
           tokens: Iterable[str] = (), walls_gone: bool = False) -> Entry:
    """Bring Soma into a room by ``door`` with a loadout, and wait until he stops moving.

    The room's flags are prepared first, with ``boss`` as ``success.prepare`` takes it, and
    ``tokens`` are the movement tokens that the loadout has. No button is pressed. The call
    returns when Soma has not changed for ``SETTLE`` frames, when he has left the room, or after
    ``LIMIT`` frames.

    ``walls_gone`` is as ``Session.enter`` takes it. An :class:`Entry` doesn't record it, and
    every stored entry was measured without it. On EmuHawk it moves the end of one arrival: by
    ``109:108`` Soma comes to rest at (112, 95), which is 32 px below his stored entry, because
    the wall that he otherwise stands on is gone.

    The game is left as the returned entry describes it. After ``"settles"`` Soma is at rest in
    the room, ``SETTLE`` frames after his last change. After ``"leaves"`` he is in another room,
    so load a saved state before going on.

    Raise :class:`~.game.GameError` if the game isn't in normal play to begin with.
    """
    tokens = tuple(tokens)
    enchant = equipped_enchant(tokens)
    target = success.room(door.partition(":")[0], session.rom)
    success.prepare(session, target, boss)
    session.set_loadout(tokens)
    session.enter(door, walls_gone)
    state = session.state()
    seen = (state.soma, state.camera, state.hp[0], state.menu_state)
    # Where Soma was on the last frame of normal play. The game moves him during a door
    # transition, before it changes the room, so a later position says nothing about the room.
    spot = state.soma
    changed, hurt = 0, False
    for frame in range(1, LIMIT + 1):
        session.step()
        state = session.state()
        if not state.in_game or state.room_pointer != target.address:
            left_by = None
            if state.in_game:
                left_by = success.exit_door(target.exits, spot, state.room_pointer)
            return Entry(door, boss, enchant, "leaves", *spot, frame, hurt, left_by)
        now = (state.soma, state.camera, state.hp[0], state.menu_state)
        if now != seen:
            changed = frame
            hurt = hurt or now[2] < seen[2]
        seen = now
        if state.playing:
            spot = state.soma
            if frame - changed >= SETTLE:
                return Entry(door, boss, enchant, "settles", *spot, changed, hurt)
    return Entry(door, boss, enchant, "unsettled", *spot, LIMIT, hurt)


def visits(rooms: Iterable[str] = (), rom: Optional[bytes] = None) -> list[tuple[str, bool]]:
    """Return every (door, boss) pair that an entry is measured for.

    That is each door of each room, and each door of an arena a second time with its boss there.
    ``rooms`` limits the answer to the rooms with those identifiers.
    """
    wanted = set(rooms)
    found = []
    for row in room_info.rows:
        if wanted and row.room_identifier not in wanted:
            continue
        target = success.room(row.room_identifier, rom)
        for boss in (False, True) if target.boss_bit is not None else (False,):
            found.extend((exit_.door, boss) for exit_ in target.exits)
    return found


def offset(entry: Entry, rom: Optional[bytes] = None) -> tuple[int, int]:
    """Return how far an entry's position is from where the door's landing has Soma stand.

    The landing is ``landing.teleport_args``'s, and Soma stands on the floor that it names. So
    ``(0, 0)`` means that the game left him exactly where the annotator's model put him.
    """
    # Imported here because reading collision needs Pillow, which nothing else here does.
    from ..annotator import landing

    spot = landing.teleport_args(entry.door, rom)
    floor = spot["floor_y"] if spot["rule"].startswith("top") else None
    return entry.x - spot["feet_x"], entry.y - ((spot["feet_y"] if floor is None else floor) - 1)


# --- The stored entries ---------------------------------------------------------------------------

def entry_path() -> str:
    """Return the path of the stored entries, which is beside this module."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ENTRY_FILE)


def entries_text(entries: Iterable[Entry]) -> str:
    """Return the text of an entry file that holds ``entries``.

    An entry with an enchant soul is left out when it ended as the same door's entry with no
    enchant soul did, because :func:`stored` gives that one in its place.
    """
    table = {entry.key: entry for entry in entries}
    lines = []
    for key in sorted(table, key=lambda key: (key[0], key[1], ENCHANTS.index(key[2]))):
        entry = table[key]
        plain = table.get((entry.door, entry.boss, ""))
        if entry.enchant and plain is not None and replace(plain, enchant=entry.enchant) == entry:
            continue
        fields = asdict(entry)
        for name, default in (("boss", False), ("enchant", ""), ("hurt", False),
                              ("left_by", None)):
            if fields[name] == default:
                del fields[name]
        lines.append(json.dumps(fields))
    return "[\n" + ",\n".join(lines) + "\n]\n"


def load_entries(path: Optional[str] = None) -> dict[tuple[str, bool, str], Entry]:
    """Return the entries of an entry file by their keys, or those of the stored file.

    The file leaves out an entry that :func:`entries_text` left out, so look an arrival up with
    :func:`stored` and not in this table.
    """
    with open(path or entry_path(), encoding="utf-8") as handle:
        rows = json.load(handle)
    # A row has no "boss" or "enchant" when the arena was empty or no enchant soul was equipped.
    found = (Entry(**{"boss": False, "enchant": "", **fields}) for fields in rows)
    return {entry.key: entry for entry in found}


@functools.lru_cache(maxsize=1)
def _stored() -> dict[tuple[str, bool, str], Entry]:
    return load_entries()


def stored(door: str, boss: bool = False, tokens: Iterable[str] = ()) -> Optional[Entry]:
    """Return the stored entry for a door and a loadout, or None if the file has none.

    The arguments are those of :func:`arrive`, and the answer is what :func:`arrive` returned
    on EmuHawk when the file was written.
    """
    enchant = equipped_enchant(tokens)
    table = _stored()
    entry = table.get((door, boss, enchant))
    if entry is None and enchant:
        plain = table.get((door, boss, ""))
        entry = replace(plain, enchant=enchant) if plain is not None else None
    return entry
