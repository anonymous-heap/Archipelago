"""
The labelled questions that an automated routing search is scored on.

A *question* asks whether a player who enters a room by one door can get to a target in that
room without leaving it. The target is another door of the room, a pickup, or an enemy placement.

A *loadout* is the set of movement tokens the player has, drawn from :data:`TOKENS`. Logic
assumes that one more token never makes a target unreachable. So the loadouts that succeed on a
question are described by a list of *ways*. A way is a set of tokens, and a loadout succeeds
when it contains every token of at least one way. The empty way makes a question free, and a
question with no way is one the sheet calls impossible.

The requirement sheets record the ways of each question. :func:`questions` reads them through
the loader that generation uses, so a question's label is the one that logic sees.

Some recorded ways need something besides tokens, which this module calls a *condition*:

* ``Enemy``, ``PixPer``, ``Clip`` and ``InZip`` are tricks, which logic allows by option.
* ``QSave`` is the quick-save reset, which puts the player back at the room's entrance. The
  pickup and enemy sheets are symmetric, so a way without ``QSave`` covers reaching the target
  and walking back to the door. A way with ``QSave`` covers only reaching the target.
* ``Floor`` and ``Ceil`` need an item that breaks a floor or a ceiling.
* ``Vert`` depends on how the player comes through a vertical door. Most ``Vert`` ways start at
  a door in the floor, where the jump that carried the player in can carry them further.
* ``Galamoth`` and ``Chronomage`` are souls outside the tokens.

A way with a condition is a *conditional* way, and a :class:`Question` keeps those apart from
its plain ways. A search that holds only tokens can neither confirm nor refute a conditional
way, so :mod:`.scoring` leaves out the loadouts that only a conditional way accepts, unless the
caller grants the condition.

About three rooms in ten are *held out* (:func:`held_out`). A search is tuned on the other rooms
and reported on the held-out ones, so that the reported numbers come from rooms the tuning never
saw. The split is by room because the questions of one room share its geometry.
"""
from __future__ import annotations

import functools
import hashlib
import pkgutil
from dataclasses import dataclass
from typing import Iterable

from ...data import entrance_info, routing_info
from ...data.routing_info import AbilityCombo
from ...regions import SPECIAL_ROOM_ENTRANCE_RULES
from ..annotator.testing import CANDIDATES

# The movement tokens a loadout is drawn from. They are the annotator's candidates, so a loadout
# means the same thing in a search as in a person's test run.
TOKENS: tuple[str, ...] = CANDIDATES
# Everything else a recorded way can need. The module docstring says what each one means.
CONDITIONS: tuple[str, ...] = tuple(
    flag.name for flag in AbilityCombo
    if flag.name not in TOKENS and flag is not AbilityCombo.Impossible)

KINDS = ("door", "pickup", "enemy")

# The reasons a question is not scored.
UNKNOWN_DOOR = "names a door that entrance_info.csv does not list"
RULE_IN_CODE = "logic replaces this row with a rule written in code"
NO_LABEL = "has no requirement that the loader can read"

# The share of layouts that are held out, and the salt of the hash that picks them.
HELD_OUT_SHARE = 0.3
_SPLIT_SALT = "cvaos-autoroute-1"


@dataclass(frozen=True)
class ConditionalWay:
    """A recorded way that needs conditions as well as tokens."""

    tokens: frozenset[str]
    conditions: frozenset[str]


@dataclass(frozen=True)
class Question:
    """One question, with the ways its sheet records.

    ``kind`` is ``"door"``, ``"pickup"`` or ``"enemy"``. ``door`` is the door the player enters
    by, as a unique door identifier such as ``"003:000"``. ``target`` is the exit door's
    identifier for a door question, and the pickup number or enemy number, as text, for the
    other two kinds. ``name`` describes the target for a person.

    ``ways`` holds the plain ways and ``conditional`` the conditional ones, both minimal. With
    neither, the sheet calls the question impossible.

    ``skip`` is empty for a question that can be scored. Otherwise it gives the reason the
    sheet's row is not a usable label, and the question is kept only so that reports can count
    it.
    """

    kind: str
    room: str
    door: str
    target: str
    name: str
    ways: tuple[frozenset[str], ...]
    conditional: tuple[ConditionalWay, ...] = ()
    skip: str = ""

    @property
    def key(self) -> str:
        """The question's name in answer files: ``kind|door|target``."""
        return f"{self.kind}|{self.door}|{self.target}"


def way_order(way: Iterable[str]) -> tuple:
    """Return a sort key that puts short ways first, then follows the order of :data:`TOKENS`."""
    positions = sorted(TOKENS.index(token) for token in way)
    return len(positions), positions


def minimal(ways: Iterable[Iterable[str]]) -> tuple[frozenset[str], ...]:
    """Return the ways that contain no other way of the list, in :func:`way_order`."""
    kept: list[frozenset[str]] = []
    for way in sorted({frozenset(way) for way in ways}, key=way_order):
        if not any(other <= way for other in kept):
            kept.append(way)
    return tuple(kept)


def ways_from_masks(masks: Iterable[int]) -> tuple[tuple[frozenset[str], ...],
                                                   tuple[ConditionalWay, ...]]:
    """Split a row's requirement masks into plain ways and conditional ways.

    ``masks`` are the loader's ``AbilityCombo`` bitmasks, one per way. A mask with
    ``Impossible`` is dropped, because that flag is never met. A conditional way is also dropped
    when a plain way needs no more tokens than it does, since the condition then adds nothing.
    """
    plain: list[frozenset[str]] = []
    conditional: dict[tuple[frozenset[str], frozenset[str]], ConditionalWay] = {}
    for mask in masks:
        if mask & int(AbilityCombo.Impossible):
            continue
        names = {flag.name for flag in AbilityCombo if mask & int(flag)}
        tokens = frozenset(names.intersection(TOKENS))
        conditions = frozenset(names.difference(TOKENS))
        if conditions:
            conditional[tokens, conditions] = ConditionalWay(tokens, conditions)
        else:
            plain.append(tokens)
    ways = minimal(plain)
    kept = [way for way in conditional.values() if not any(other <= way.tokens for other in ways)]
    kept.sort(key=lambda way: (way_order(way.tokens), sorted(way.conditions)))
    return ways, tuple(kept)


def _question(kind: str, room: str, door: str, target: str, name: str, masks: tuple[int, ...],
              skip: str = "") -> Question:
    ways, conditional = ways_from_masks(masks)
    if not masks and not skip:
        skip = NO_LABEL
    return Question(kind, room, door, target, name, ways, conditional, skip)


@functools.lru_cache(maxsize=1)
def questions() -> tuple[Question, ...]:
    """Return every question the sheets hold: the door rows, then the pickups, then the enemies.

    A door row gives one question. A pickup or enemy row gives one question for each door that
    the loader resolves it to, which are the pairs that logic connects.

    Three kinds of row are not usable as labels, and their questions carry the reason in
    ``skip``: a row that names a door missing from ``entrance_info.csv``, a door row that
    ``regions.SPECIAL_ROOM_ENTRANCE_RULES`` replaces, and a row whose only requirement is text
    that the loader cannot read.
    """
    doors = entrance_info.by_door_identifier_unique
    out = []
    for row in routing_info.rows:
        door = f"{row.room_id}:{row.from_room}"
        target = f"{row.room_id}:{row.to_room}"
        skip = ""
        if door not in doors or target not in doors:
            skip = UNKNOWN_DOOR
        elif (row.room_id, row.from_room, row.to_room) in SPECIAL_ROOM_ENTRANCE_RULES:
            skip = RULE_IN_CODE
        out.append(_question("door", row.room_id, door, target, f"door to {row.to_room}",
                             row.get_requirement_bitmasks(), skip))
    for pickup in routing_info.pickup_region_rows:
        door = pickup.entrance_identifier
        out.append(_question("pickup", pickup.pickup_room, door, str(pickup.pickup_number),
                             pickup.item_name, pickup.get_requirement_bitmasks(),
                             "" if door in doors else UNKNOWN_DOOR))
    for enemy in routing_info.enemy_region_rows:
        door = enemy.entrance_identifier
        name = f"{enemy.enemy_name} {enemy.specifier}".strip()
        out.append(_question("enemy", enemy.enemy_room, door, str(enemy.enemy_number), name,
                             enemy.get_requirement_bitmasks(),
                             "" if door in doors else UNKNOWN_DOOR))
    return tuple(out)


# --- The held-out rooms ---------------------------------------------------------------------------

HELD_OUT_FILE = "held_out_rooms.txt"


@functools.lru_cache(maxsize=1)
def held_out_rooms() -> frozenset[str]:
    """Return the held-out rooms, from ``held_out_rooms.txt`` beside this module."""
    text = pkgutil.get_data(__name__, HELD_OUT_FILE).decode("utf-8-sig")
    lines = (line.strip() for line in text.splitlines())
    return frozenset(line for line in lines if line and not line.startswith("#"))


def held_out_text(rooms: Iterable[str]) -> str:
    """Return the contents of ``held_out_rooms.txt`` for a list of rooms."""
    head = ("# The rooms held out of tuning, one on each line. benchmark.choose_held_out picks\n"
            "# them, and this file is written by:\n"
            "#     python -m worlds.cvaos.tools.autoroute split --write\n")
    return head + "".join(f"{room}\n" for room in sorted(rooms))


def held_out(room: str) -> bool:
    """Return True when ``room`` is held out. A room missing from the list is not."""
    return room in held_out_rooms()


def layout_groups(rom: bytes) -> list[list[str]]:
    """Group the rooms of ``entrance_info.csv`` by collision, so that equal layouts are together.

    Rooms with the same collision grid would leak between the two sides of the split. The Boss
    Rush rooms are copies of the boss rooms, for example. Each group is sorted, and the groups
    are in the order of their first rooms.
    """
    # Imported here because reading collision needs Pillow, which nothing else in this module does.
    from ..annotator import landing

    castle = landing._castle(rom)
    by_layout: dict[str, set[str]] = {}
    for door in entrance_info.rows:
        collision = castle.collision(door.room_address)
        layout = repr((collision.width, collision.height, collision._kinds, collision._masks))
        digest = hashlib.sha256(layout.encode()).hexdigest()
        by_layout.setdefault(digest, set()).add(door.room_identifier)
    return sorted(sorted(rooms) for rooms in by_layout.values())


def choose_held_out(groups: Iterable[Iterable[str]]) -> list[str]:
    """Return the rooms of the groups that a salted hash of each group's first room picks.

    The hash picks about :data:`HELD_OUT_SHARE` of the groups, and a group is held out whole.
    ``held_out_rooms.txt`` is this function's result for :func:`layout_groups` of the US ROM.
    """
    chosen: list[str] = []
    for group in groups:
        rooms = sorted(group)
        digest = hashlib.sha256(f"{_SPLIT_SALT}:{rooms[0]}".encode()).digest()
        if int.from_bytes(digest[:8], "big") < HELD_OUT_SHARE * 2 ** 64:
            chosen.extend(rooms)
    return sorted(chosen)
