"""
The reference scenarios: short pieces of play, recorded on EmuHawk for other emulators to match.

A search can run on a faster emulator than EmuHawk only if that emulator plays the same game.
EmuHawk is the reference, because the annotator's testers play on it. A **scenario** is a
function that plays one piece of the game through a ``traces.Recording``. Its trace then holds
every read that the piece made and the bytes that EmuHawk returned. Replaying the trace on
another emulator (``traces.replay``) shows the first read at which the two differ.

Each scenario starts with a reset, so its trace replays on its own. A scenario may choose its
buttons from what it reads, as a person would. Its trace still replays exactly, because a replay
repeats the recorded operations and doesn't run the scenario again.

:func:`run` plays a scenario by name and returns its trace. :func:`about` returns the
description that goes on the first line of the trace's file. The command
``python -m worlds.cvaos.tools.autoroute trace`` records the files and replays them.
"""
from __future__ import annotations

import hashlib
import random
from typing import Callable, Iterable, Optional, Sequence

from ..annotator import emulator as annotator
from ..annotator import settings
from ..annotator.testing import CANDIDATES
from . import entries, game, success, traces
from .emulator import Emulator, read_many

# The emulator that the recorded traces come from, with the settings of its core that change
# what the game does. Every trace file carries this on its first line.
REFERENCE = {
    "emulator": "EmuHawk 2.11.1",
    "core": "mGBA",
    "bios": "the core's built-in BIOS, skipped at boot",
    "SkipBios": True,
    "RTCUseRealTime": True,
    "RTCInitialTime": "2010-01-01",
    "OverrideSaveType": -1,
    "save": "blank at every reset",
}

# What a scenario reads after each frame it plays: the game's state, and Soma's slot of the
# entity table, which holds his position, his speed and what he is doing.
SOMA_RECORD = (game.ENTITY_BASE, game.ENTITY_SIZE)
FRAME_READS = (*annotator.STATE_READS, SOMA_RECORD)
ABILITIES = tuple(token for token in CANDIDATES if token in annotator.ABILITY_SOULS)

# A script is a sequence of (buttons, frames) pairs: the buttons held, and for how many frames.
Script = Sequence[tuple[tuple[str, ...], int]]


class ScenarioError(Exception):
    """A scenario didn't go the way it is written, so its trace wouldn't show what it is for."""


def _play(session: game.Session, script: Script) -> list[annotator.GameState]:
    """Run a script one frame at a time, and read :data:`FRAME_READS` after each frame.

    Return the game's state after each frame.
    """
    states = []
    for buttons, frames in script:
        for _ in range(frames):
            session.step(buttons)
            blocks = read_many(session.emulator, FRAME_READS)
            states.append(annotator.parse_state(blocks[:len(annotator.STATE_READS)]))
    return states


def _arrive(tape: traces.Recording, session: game.Session, door: str, boss: bool = False,
            tokens: Iterable[str] = ()) -> entries.Entry:
    """Bring Soma in by a door, and raise unless the arrival ends as the entry file says."""
    tokens = tuple(tokens)
    entry = entries.arrive(session, door, boss, tokens)
    stored = entries.stored(door, boss, tokens)
    if entry != stored:
        raise ScenarioError(f"The arrival by {door} gave {entry}, and the entry file has"
                            f" {stored}.")
    tape.note(f"arrived by {door}{' with its boss' if boss else ''}"
              f"{' and ' + entry.enchant if entry.enchant else ''}: {entry.outcome} at"
              f" {entry.x},{entry.y} after {entry.frames} frames")
    return entry


def _hunt(session: game.Session, watch: success.Watch, direction: str,
          wanted: Iterable[success.Event], limit: int) -> int:
    """Walk one way and attack twice a second until the watch has reported the wanted events.

    The watch looks after every frame. Return the frames it took, or raise
    :class:`ScenarioError` after ``limit`` frames.
    """
    wanted = set(wanted)
    for frame in range(limit):
        phase = frame % 30
        session.step(("B",) if phase == 0 else () if phase < 12 else (direction,))
        watch.look()
        if wanted <= set(watch.events):
            return frame + 1
        if watch.over:
            break
    missing = sorted(event.target for event in wanted - set(watch.events))
    raise ScenarioError(f"After {limit} frames the watch had not reported {missing}.")


# --- The scenarios --------------------------------------------------------------------------------

def boot(tape: traces.Recording, session: game.Session) -> None:
    """A reset, the frames until normal play starts, and two seconds of standing still.

    The game's state is read after every frame of the boot. Both blocks of work RAM are
    digested when play starts and again at the end.
    """
    frames = session.boot()
    tape.note(f"normal play started {frames} frames after the reset")
    tape.digest(*traces.EWRAM)
    tape.digest(*traces.IWRAM)
    for _ in range(4):
        _play(session, [((), 30)])
        tape.digest(*traces.EWRAM)
    tape.digest(*traces.IWRAM)


# The arrivals that the scenario below makes, as (door, boss, enchant soul). There is one for
# each way a door can treat Soma: a side door, a door in the ceiling, two kinds of door in the
# floor, a wooden door, an arena with its boss, water with and without Dive, a wall that he
# stands on, a room where Water Walk changes where he ends, and a platform that keeps moving.
ARRIVALS = (
    ("000:003", False, ""), ("009:008", False, ""), ("008:009", False, ""),
    ("002:003", False, ""), ("003:000", False, ""), ("00A:009", True, ""),
    ("704:708", False, ""), ("704:708", False, "Dive"), ("712:711", False, ""),
    ("712:711", False, "Dive"), ("109:108", False, ""), ("721:719", False, "WWalk"),
    ("503:502 (left)", False, ""),
)


def arrivals(tape: traces.Recording, session: game.Session) -> None:
    """An arrival by each kind of door, every one from the state that follows the boot.

    The game's state is read after every frame of an arrival, and all of EWRAM is digested at
    the end of each one.
    """
    session.boot()
    base = tape.save()
    for door, boss, enchant in ARRIVALS:
        tape.load(base)
        _arrive(tape, session, door, boss, [enchant] if enchant else [])
        tape.digest(*traces.EWRAM)
    tape.load(base)
    tape.drop(base)


# The script of the movement scenario, with pauses in which Soma comes to rest. Its lines are:
# a walk; a running jump; a jump and the double jump (A again in the air); two jumps to the
# right; the guardian soul on the ground (R), then Up, which the Bat flies with; the guardian
# soul in the air, where Glide works; a turn to the right; the slide (Down and A on the ground);
# the backdash (L); the high jump (Up and L); the kick (Down and A after a double jump); an
# attack and the bullet soul's buttons; and a walk with a jump and attacks.
MOVES: Script = (
    (("Left",), 60), ((), 10),
    (("Left", "A"), 24), (("Left",), 40), ((), 10),
    (("A",), 14), ((), 4), (("A",), 14), ((), 50),
    (("Right", "A"), 10), (("Right",), 6), (("Right", "A"), 20), (("Right",), 30), ((), 30),
    (("Left", "R"), 30), (("Up",), 20), ((), 30),
    (("A",), 14), (("R",), 30), ((), 40),
    (("Right",), 20), ((), 10),
    (("Down",), 20), (("Down", "A"), 3), (("Down",), 30), ((), 10),
    (("L",), 3), ((), 30),
    (("Up", "L"), 10), ((), 130),
    (("A",), 14), ((), 4), (("A",), 14), ((), 6), (("Down", "A"), 6), ((), 40),
    (("B",), 3), ((), 30), (("Up", "B"), 3), ((), 40),
    (("Left", "B"), 3), (("Left",), 20), (("Left", "A"), 10), (("Left", "A", "B"), 3),
    (("Left",), 20), ((), 20),
)
# Soma starts at the right end of room 000, which is large and has no enemy. Its only door is
# the one he comes in by, and the script keeps him in the room with every loadout.
MOVE_DOOR = "000:003"
# A person stands at x 138 on the floor of room 000. Pressing Up within about 25 px of them
# starts a talk, and Soma then answers to no button until A has been pressed about 116 times.
# The script presses Up only to the right of this x, and the scenario checks that.
TALK_X = 180
# The loadouts that the movement scenario plays the script with. A loadout equips one guardian
# soul and one enchant soul, so these five cover every movement soul between them.
MOVE_LOADOUTS = ((), CANDIDATES, (*ABILITIES, "Bat", "Dive"), (*ABILITIES, "Panth"),
                 (*ABILITIES, "Tform"))


def movement(tape: traces.Recording, session: game.Session) -> None:
    """One fixed script of running, jumping and soul use in room 000, with five loadouts.

    The game's state and Soma's own record are read after every frame. MP is kept full, because
    the guardian souls use it.
    """
    session.boot()
    base = tape.save()
    room = success.room(MOVE_DOOR.partition(":")[0], session.rom)
    for tokens in MOVE_LOADOUTS:
        loadout = " ".join(tokens) or "no souls"
        tape.load(base)
        tape.note(f"the movement script with {loadout}")
        _arrive(tape, session, MOVE_DOOR, tokens=tokens)
        session.keep_vitals_full()
        for hold in MOVES:
            played = _play(session, [hold])
            if any(state.room_pointer != room.address for state in played):
                raise ScenarioError(f"The movement script took Soma out of room"
                                    f" {room.identifier} with {loadout}.")
            if "Up" in hold[0] and min(state.soma[0] for state in played) < TALK_X:
                raise ScenarioError(f"The movement script pressed Up beside the person in room"
                                    f" {room.identifier} with {loadout}.")
        tape.digest(*traces.EWRAM)
    tape.load(base)
    tape.drop(base)


# The runs of the random scenario, as (door, loadout, the direction that leads into the room),
# and the number of frames in each.
RANDOM_RUNS = (("009:006", CANDIDATES, "Right"), ("704:708", ("Dive",), "Left"),
               ("303:307", (), "Left"))
RANDOM_FRAMES = 1200
RANDOM_SEED = "cvaos-autoroute-reference-1"


def _random_script(rng: random.Random, frames: int,
                   inward: str) -> list[tuple[tuple[str, ...], int]]:
    """Return a script of random holds of 1 to 8 frames that lasts ``frames`` frames.

    ``inward`` is Left or Right, and a hold has it twice as often as the other of the two, so
    that Soma tends to go into the room. A hold never has both of them, and never Start or
    Select, which open menus.
    """
    outward = "Left" if inward == "Right" else "Right"
    script, left = [], frames
    while left > 0:
        held = [rng.choice(("", "", inward, inward, outward)),
                rng.choice(("", "", "", "Up", "Down"))]
        held += [button for button, chance in (("A", 0.35), ("B", 0.15), ("L", 0.1), ("R", 0.2))
                 if rng.random() < chance]
        length = min(left, rng.randint(1, 8))
        script.append((tuple(button for button in held if button), length))
        left -= length
    return script


def random_play(tape: traces.Recording, session: game.Session) -> None:
    """Twenty seconds of random buttons from each of three doors.

    The buttons come from a seeded generator, and each hold lasts 1 to 8 frames. The game's
    state and Soma's own record are read after every frame, and all of EWRAM is digested about
    every 100 frames. Soma may be hit or leave the room on the way, and the run goes on.
    """
    session.boot()
    base = tape.save()
    for door, tokens, inward in RANDOM_RUNS:
        tape.load(base)
        tape.note(f"random buttons from {door} with {' '.join(tokens) or 'no souls'}")
        _arrive(tape, session, door, tokens=tokens)
        session.keep_vitals_full()
        script = _random_script(random.Random(f"{RANDOM_SEED}/{door}"), RANDOM_FRAMES, inward)
        since = 0
        for hold in script:
            _play(session, [hold])
            since += hold[1]
            if since >= 100:
                tape.digest(*traces.EWRAM)
                since = 0
        tape.digest(*traces.EWRAM)
    tape.load(base)
    tape.drop(base)


def leaving(tape: traces.Recording, session: game.Session) -> None:
    """Soma walks out of room 603 by the door he came in by, and the watch names that door.

    The watch looks after every frame until Soma is in the next room. The game's state is then
    read for 20 more frames, which cover the end of the door transition.
    """
    session.boot()
    _arrive(tape, session, "603:606")
    watch = success.Watch(session, success.room("603", session.rom))
    for _ in range(120):
        session.step(("Right",))
        watch.look()
        if watch.over:
            break
    if watch.over != "left" or watch.events != [success.Event("door", "603:606", "left")]:
        raise ScenarioError(f"Soma should have left by 603:606, and the watch has {watch.over}"
                            f" with {watch.events}.")
    tape.note("the watch reported the door 603:606 as left")
    _play(session, [((), 20)])


# The script of the pickup scenario. Soma walks along the platform that he arrives on until a
# wall stops him above the pickup, and then drops through the platform.
TO_PICKUP: Script = ((("Left",), 110), (("Down",), 6), (("Down", "A"), 4), ((), 60))


def pickup(tape: traces.Recording, session: game.Session) -> None:
    """Soma drops through a platform of room 603 onto pickup 67, and the watch reports it.

    The watch looks after every frame until the game marks the pickup as collected. The game's
    state is then read for 90 more frames, in which the game shows what he picked up.
    """
    session.boot()
    _arrive(tape, session, "603:606")
    watch = success.Watch(session, success.room("603", session.rom))
    wanted = success.Event("pickup", "67", "collected")
    found = False
    for buttons, frames in TO_PICKUP:
        for _ in range(frames):
            if not found:
                session.step(buttons)
                found = wanted in watch.look()
    if not found:
        raise ScenarioError(f"Soma didn't collect pickup 67 of room 603, and he is at"
                            f" {session.state().soma}.")
    tape.note("the watch reported pickup 67 as collected")
    _play(session, [((), 90)])


def defeat(tape: traces.Recording, session: game.Session) -> None:
    """Soma defeats the Zombie Soldier of room 02C, the only enemy of its type there.

    He has the annotator's test weapon and strength, and his HP and MP are kept full. The watch
    looks after every frame, and reports the defeat from the game's defeated bits alone.
    """
    session.boot()
    _arrive(tape, session, "02C:02B")
    session.equip_test_weapon()
    session.keep_strength_up()
    session.keep_vitals_full()
    watch = success.Watch(session, success.room("02C", session.rom))
    frames = _hunt(session, watch, "Right", [success.Event("enemy", "96", "defeated")], 900)
    tape.note(f"the watch reported enemy 96 as defeated after {frames} frames")
    _play(session, [((), 30)])


# The Fleaman placements of room 303, which stand within 16 px of each other.
FLEAMEN = ("206", "207", "209", "210")


def twins(tape: traces.Recording, session: game.Session) -> None:
    """Soma defeats two of the four Fleamen of room 303, which are placements of one type.

    The defeated bits can't say which placement a defeated Fleaman came from. So the watch also
    reads the entity table on the frames where the type's bit is set, and it clears the bit
    after each defeat. The trace holds those reads and writes.
    """
    session.boot()
    _arrive(tape, session, "303:307")
    session.equip_test_weapon()
    session.keep_strength_up()
    session.keep_vitals_full()
    watch = success.Watch(session, success.room("303", session.rom))
    down: list[str] = []
    frames = 0
    while len(down) < 2 and not watch.over and frames < 1500:
        phase = frames % 30
        session.step(("B",) if phase == 0 else () if phase < 12 else ("Left",))
        frames += 1
        watch.look()
        down = [event.target for event in watch.events
                if event.how == "defeated" and event.target in FLEAMEN]
    if len(down) < 2:
        raise ScenarioError(f"The watch credited the Fleamen {down} of room 303, and the"
                            f" scenario needs two. Uncredited types: {watch.uncredited}.")
    tape.note(f"the watch reported the Fleamen {' and '.join(down)} as defeated after"
              f" {frames} frames, with uncredited types {watch.uncredited}")
    _play(session, [((), 30)])


def states(tape: traces.Recording, session: game.Session) -> None:
    """Saved states: a load restores the game, and play from a loaded state repeats itself.

    Two states are saved, one in the middle of the play that follows the other. The scenario
    digests EWRAM after each load and after each piece of play, and raises when a digest
    differs from the one that the same point gave before. So the trace holds equal digests
    wherever an emulator's saved states have to give equal memory.
    """
    session.boot()
    _arrive(tape, session, MOVE_DOOR, tokens=CANDIDATES)
    session.keep_vitals_full()
    # The walk and the jumps of the movement script, and then its lines up to the guardian soul
    # in the air.
    first, second = MOVES[:9], MOVES[9:20]

    def check(what: str, expected: Optional[str] = None) -> str:
        found = tape.digest(*traces.EWRAM)
        if expected is not None and found != expected:
            raise ScenarioError(f"EWRAM {what} differs from the first time.")
        return found

    one = tape.save()
    at_one = check("at the first state")
    _play(session, first)
    at_two = check("after the first piece of play")
    tape.load(one)
    check("after loading the first state", at_one)
    _play(session, first)
    check("after playing the first piece again", at_two)
    two = tape.save()
    _play(session, second)
    at_end = check("after the second piece of play")
    tape.load(two)
    check("after loading the second state", at_two)
    _play(session, second)
    check("after playing the second piece again", at_end)
    tape.load(one)
    check("after loading the first state a second time", at_one)
    tape.drop(two)
    tape.drop(one)


SCENARIOS: dict[str, Callable[[traces.Recording, game.Session], None]] = {
    "boot": boot, "arrivals": arrivals, "movement": movement, "random": random_play,
    "leaving": leaving, "pickup": pickup, "defeat": defeat, "twins": twins, "states": states,
}


def run(name: str, emulator: Emulator, rom: Optional[bytes] = None) -> list[list]:
    """Play the scenario called ``name`` on ``emulator``, and return its trace.

    ``rom`` is the whole US ROM, which the scenarios need for the rooms' collision. The
    annotator's copy is read when it is None. Raise :class:`ScenarioError` if the play didn't go
    the way the scenario is written.
    """
    tape = traces.Recording(emulator)
    SCENARIOS[name](tape, game.Session(tape, rom))
    return tape.ops


def about(name: str, rom: Optional[bytes] = None) -> dict:
    """Return the description of a scenario's trace, for the first line of its file.

    It names the scenario, says what it plays, and gives the md5 of the test ROM that the trace
    is for, which is the annotator's patched copy of the US ROM. :data:`REFERENCE` is included
    under ``"reference"``, so only use this for a trace that EmuHawk made.
    """
    if rom is None:
        with open(settings.us_rom_path(), "rb") as handle:
            rom = handle.read()
    summary = (SCENARIOS[name].__doc__ or "").strip().partition("\n")[0]
    return {"scenario": name, "about": summary,
            "test_rom_md5": hashlib.md5(annotator.build_test_rom(rom)).hexdigest(),
            "reference": REFERENCE}
