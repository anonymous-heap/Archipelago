"""
The success tests: whether Soma left by a door, picked up an item, or reached or defeated an enemy.

A question in ``benchmark.py`` asks whether Soma can get from a door of a room to a target in the
same room. The target is another door, a pickup, or an enemy placement. Each kind of target has
its own test here, and each test is a function of values read from the game's memory:

* :func:`exit_door` names the door Soma left a room by.
* :func:`collected` and :func:`touching` say whether Soma has picked a pickup up.
* :func:`reached` and :func:`defeated` say whether Soma got to an enemy placement, or defeated
  the enemy it spawns.

:class:`Room` gathers every target of one room, and :class:`Watch` applies the tests to all of
them while Soma moves about the room. So one exploration from a door can answer every question
that starts at that door, and each :class:`Event` carries the key of the question it answers.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import NamedTuple, Optional, Sequence

from ...data import enemy_info, pickup_info, room_info, routing_info
from ..annotator import emulator as annotator
from ..annotator.emulator import GameState
from ..annotator.testing import REACH_DISTANCE
from . import game
from .emulator import read_many

HARD_PICKUP = 5   # the type of a pickup that the game places only in Hard Mode
# The numbers of the placements that generate enemies with no record of their parent, in a room
# with several such placements of one type. Both are Medusa Head generators at one spot of room
# 615. Nothing in the game's memory says which of the two a head came from, so a defeated head
# counts for both.
UNLINKED_GENERATORS = frozenset({449, 450})
# Where Soma's feet are when he is touching a pickup: at most TOUCH_SIDE pixels to either side
# of it, and a number of pixels below it that lies in the TOUCH_BELOW range, ends included.
#
# The game collects a pickup when a standing or falling Soma has his feet 7 to 47 pixels below
# it, and from 14 pixels to its left to 13 to its right. It doesn't collect one when his feet
# are level with it or above it. Those bounds are measurements on pickups 67 and 110, taken by
# writing his position. The box here is the lower half of that range, with a pixel or two to
# spare at the sides, because Soma's body is shorter in his other postures and the game's range
# for those is not measured.
TOUCH_SIDE = 12
TOUCH_BELOW = (7, 24)


@dataclass(frozen=True)
class Pickup:
    """An item pickup in a room."""

    number: int            # its pickup number, which a question's target holds as text
    name: str
    x: int                 # in room pixels
    y: int
    flag: Optional[int]    # its bit in the collected-pickup flags, or None if it has no flag
    spawns: bool           # False for a pickup that the game places only in Hard Mode


@dataclass(frozen=True)
class Enemy:
    """An enemy placement in a room."""

    number: int              # its enemy number, which a question's target holds as text
    name: str
    type_id: int
    x: int                   # its spawn point, in room pixels
    y: int
    # The id of the room entry that spawns it, which its entity carries. None when the ROM has
    # no entry of this type at this spot.
    entry_id: Optional[int]
    # True when the room has another placement of the same type. The game's defeated bit is per
    # type, so then only the entity table can show which placement was defeated (see dying).
    twins: bool
    # True for the boss of a boss arena, which is there only while the arena's boss flag is clear.
    boss: bool


@dataclass(frozen=True)
class Room:
    """Every target in one room, and the flags that decide which of them are there."""

    identifier: str   # such as "90D"
    address: int
    exits: tuple      # of landing.Exit, one for each door out of the room
    pickups: tuple[Pickup, ...]
    enemies: tuple[Enemy, ...]
    # The flags that :func:`prepare` sets, as (address of the flag block, flag number) pairs.
    flags: tuple[tuple[int, int], ...]
    # The bit of the boss flags for the boss fought in this room, or None if it isn't an arena.
    boss_bit: Optional[int]

    def present(self, boss: bool = False) -> tuple[Enemy, ...]:
        """Return the enemies to look for after :func:`prepare` with the same ``boss``.

        Those are all the enemies of an ordinary room. In an arena they are the boss while the
        boss flag is clear, and the other enemies once it is set, because the arena holds its
        other enemies only after the fight.
        """
        if self.boss_bit is None:
            return self.enemies
        return tuple(enemy for enemy in self.enemies if enemy.boss == boss)


@functools.lru_cache(maxsize=None)
def room(identifier: str, rom: Optional[bytes] = None) -> Room:
    """Return the targets of the room with this identifier.

    ``rom`` is the whole US ROM, and the annotator's copy is read when it is None. Raise
    KeyError for an identifier that names no room.
    """
    # Imported here because reading collision needs Pillow, which nothing else here does.
    from ..annotator import landing

    if rom is None:
        rom = landing._default_rom()
    address = room_info.by_identifier[identifier].room_address
    pickups = tuple(
        Pickup(row.pickup_number, row.simple_name, row.x, row.y,
               row.flag_offset if row.flag_offset > 0 else None, row.type_num != HARD_PICKUP)
        for row in pickup_info.rows if row.room_identifier == identifier)
    placements = [row for row in routing_info.enemy_placements if row.room == identifier]
    flags, boss_bit = annotator.room_flags(rom, address)
    enemies = []
    for row in placements:
        type_id = enemy_info.by_name[row.name].id
        twins = any(other.name == row.name and other.enemy_number != row.enemy_number
                    for other in placements)
        # Two placements of one type at one spot have an entry each. They take the entries in
        # the order of their enemy numbers, so that each gets a different one.
        earlier = sum(other.name == row.name and (other.x, other.y) == (row.x, row.y)
                      and other.enemy_number < row.enemy_number for other in placements)
        entry = annotator.room_entry(rom, address, type_id, row.x, row.y, index=earlier)
        # The only enemy entries of an arena are its boss. Its other enemies are spawned by
        # objects that wait for the boss flag.
        boss = (boss_bit is not None and entry is not None
                and entry.kind == annotator.ENEMY_ENTRY)
        enemies.append(Enemy(row.enemy_number, row.name, type_id, row.x, row.y,
                             entry.entry_id if entry else None, twins, boss))
    return Room(identifier, address, tuple(landing.exits(address, rom)), pickups, tuple(enemies),
                tuple(flags), boss_bit)


# --- The tests ------------------------------------------------------------------------------------

def _distance_to_line(point: tuple[int, int], line: tuple[int, int, int, int]) -> float:
    """Return the distance from a point to an axis-aligned line segment."""
    x0, y0, x1, y1 = line
    dx = point[0] - min(max(point[0], x0), x1)
    dy = point[1] - min(max(point[1], y0), y1)
    return (dx * dx + dy * dy) ** 0.5


def exit_door(exits: Sequence, soma: tuple[int, int], new_room: int) -> Optional[str]:
    """Return the door that Soma left a room by, or None if no door of the room explains it.

    ``exits`` are the room's doors, as ``landing.exits`` returns them. ``soma`` is where his feet
    were when he was last seen in the room, in room pixels, and ``new_room`` is the address of the
    room he is in now.

    A door explains the move when it leads to ``new_room``. When several doors do, the answer is
    the one whose line is nearest to ``soma``.
    """
    leading = [door for door in exits if door.dest_room_address == new_room]
    if len(leading) <= 1:
        return leading[0].door if leading else None
    placed = [door for door in leading if door.line is not None]
    if not placed:
        return None
    return min(placed, key=lambda door: _distance_to_line(soma, door.line)).door


def collected(flags: bytes, pickup: Pickup) -> bool:
    """Whether the game has marked ``pickup`` as collected.

    ``flags`` are the collected-pickup flags (``game.PICKUP_FLAGS``). A pickup without a flag is
    never marked.
    """
    if pickup.flag is None:
        return False
    return bool(flags[pickup.flag // 8] & (1 << (pickup.flag % 8)))


def touching(soma: tuple[int, int], pickup: Pickup) -> bool:
    """Whether Soma's feet are where the game would collect ``pickup``.

    This stands in for :func:`collected` where the game doesn't place the pickup, so that there
    is nothing to collect. It is true only inside a box where the game is known to collect a
    pickup, so it can miss a touch but doesn't report one that the game wouldn't.
    """
    low, high = TOUCH_BELOW
    return abs(soma[0] - pickup.x) <= TOUCH_SIDE and low <= soma[1] - pickup.y <= high


def reached(soma: tuple[int, int], enemy: Enemy) -> bool:
    """Whether Soma's feet are within the annotator's reach distance of the enemy's spawn point."""
    return (soma[0] - enemy.x) ** 2 + (soma[1] - enemy.y) ** 2 <= REACH_DISTANCE ** 2


def _bit(defeated_bits: bytes, type_id: int) -> bool:
    return bool(defeated_bits[type_id >> 3] & (1 << (type_id & 7)))


def dying(entities: Sequence[game.Entity], enemy: Enemy) -> bool:
    """Whether ``entities`` hold a defeated enemy that counts for this placement.

    A defeated enemy has HP below 0. The game keeps its entity for a short time and then removes
    it. That time was 3 frames for the quickest enemy measured and about 140 for the slowest,
    and it is about 40 for most. So the answer is True only for a short time after a defeat.

    A defeated enemy counts for the placement in three cases:

    * It is the placement's own entity, which is the one that the placement's room entry
      spawned. That entity carries the entry's id.
    * It records the placement's own entity as its parent. A head of a Bone Pillar or of a Fish
      Head counts for its stack in this way, and a Ghost counts for its generator.
    * It records no parent, the placement is one of :data:`UNLINKED_GENERATORS`, and the
      placement's own entity is in ``entities``.

    The answer is False while the placement's own entity isn't in ``entities``, because a
    missing entity shows nothing either way. The game leaves an enemy out until the camera is
    near its spawn point, and it removes some enemies without a defeat once they are off the
    screen.
    """
    if enemy.entry_id is None:
        return False
    # An entity that another entity made carries entry id 0 too, so that id alone doesn't pick out
    # the own entity of a placement whose entry has id 0. The own entity records no parent, and
    # that leaves out the made entities that record one.
    own = {entity.slot for entity in entities
           if entity.slot in game.ROOM_SLOTS and entity.type_id == enemy.type_id
           and entity.entry_id == enemy.entry_id
           and (enemy.entry_id != 0 or entity.parent is None)}
    if not own:
        return False
    for entity in entities:
        if entity.slot == 0 or entity.type_id != enemy.type_id or entity.hp >= 0:
            continue
        if entity.slot in own:
            return True
        if entity.entry_id == 0 and (entity.parent in own or (
                entity.parent is None and enemy.number in UNLINKED_GENERATORS)):
            return True
    return False


def defeated(entities: Sequence[game.Entity], defeated_bits: bytes, enemy: Enemy) -> bool:
    """Whether the enemy that a placement spawns has been defeated.

    ``entities`` are the live entities, and ``defeated_bits`` are the game's defeated bits, one
    for each enemy type (``GameState.defeated``).

    Where the room has one placement of the type, the type's bit shows the defeat, and it stays
    set. The bit must be clear when Soma enters the room, which :func:`prepare` sees to.

    Where the room has several placements of the type, the bit doesn't say which of them was
    defeated. The answer then comes from the entities (see :func:`dying`), so it is True only
    for a short time after the defeat.
    """
    if enemy.twins:
        return dying(entities, enemy)
    return _bit(defeated_bits, enemy.type_id)


# --- Watching a room ------------------------------------------------------------------------------

def prepare(session: game.Session, target: Room, boss: bool = False) -> None:
    """Set the game's flags so that a room's targets are there when Soma enters it.

    This clears the collected flag of each pickup and the defeated bit of each enemy type. It
    also sets the flags in ``target.flags``. The room's event scenes then don't play, and the
    enemies that wait for an event or for another room's boss are there.

    A boss arena has two states, and ``boss`` chooses between them. By default the arena's boss
    flag is set, so the boss and the boss doors are gone and the arena's other enemies are
    there. With ``boss`` true the flag is cleared, so the boss is there and the doors hold Soma
    in. Answering every question about an arena therefore takes two explorations.
    ``target.present(boss)`` lists the enemies of each. ``boss`` changes nothing in a room that
    isn't an arena.

    Call this before the session enters the room, because the game reads the flags when it loads
    the room.
    """
    for pickup in target.pickups:
        if pickup.flag is not None:
            session.set_flag(game.PICKUP_FLAGS[0], pickup.flag, False)
    for enemy in target.enemies:
        session.set_flag(annotator.ENEMY_DEFEATED, enemy.type_id, False)
    for address, number in target.flags:
        session.set_flag(address, number, True)
    if target.boss_bit is not None:
        session.set_flag(annotator.BOSS_FLAGS, target.boss_bit, not boss)


class Event(NamedTuple):
    """Something Soma achieved in a room.

    ``kind`` and ``target`` are those of the benchmark's questions. For a door, ``target`` is the
    identifier of the door Soma left by. For a pickup or an enemy, it is the pickup or enemy
    number as text. ``how`` is "left", "collected", "touched", "reached" or "defeated".
    """

    kind: str
    target: str
    how: str

    def key(self, door: str) -> str:
        """Return the key of the question that this answers for an entry by ``door``."""
        return f"{self.kind}|{door}|{self.target}"


class Watch:
    """Follows Soma through one room and reports what he achieves there.

    Call :meth:`look` after stepping the game, as often as the search wants an answer. Each call
    reads the game once and returns the events that are new since the call before. An event is
    reported once, however long it stays true.

    ``boss`` is the value that :func:`prepare` was given for this visit. It decides which
    enemies of an arena the watch looks for (see :meth:`Room.present`).

    ``over`` is None while Soma is in the room. It becomes "left" when he leaves by a door. It
    becomes "gone" when he leaves in a way that no door explains, or when the game stops running
    the castle, as it does at a game over. A menu or a door transition doesn't end the watch.
    After that, :meth:`look` reads nothing and returns no events. A watch belongs to one attempt,
    so make a new one after loading a saved state.

    A defeat shows first in the game's defeated bits, which hold one bit for each enemy type.
    Where the room has one placement of the type, the bit settles which placement it was. Where
    it has several, the look that finds the bit set reads the entity table as well, and credits
    each placement that the table shows a defeated enemy for (see :func:`dying`). It then clears
    the bit, so that the next defeat of the type sets it again. That is the only thing a watch
    writes.

    The table shows a defeated enemy only until the game removes its entity, which can be as
    soon as 3 frames later. A look that comes after that finds the bit set and nothing in the
    table to explain it. It then reports no event and adds the type to ``uncredited``. The
    enemy doesn't come back while Soma stays in the room, so that placement can't be credited
    later in the same attempt. So look on every frame, or on every second one, while an attack
    of Soma's can land where a type has several placements.

    ``uncredited`` also lists defeats that no placement can be credited for at any look. The
    Unes of rooms 72B and 72F are the known case: their defeats haven't been measured, so
    :func:`dying` has no rule for them.
    """

    def __init__(self, session: game.Session, target: Room, boss: bool = False) -> None:
        self.session = session
        self.room = target
        self.enemies = target.present(boss)
        self.over: Optional[str] = None
        self.events: list[Event] = []          # every event so far, in order
        # The enemy type of each defeat that no placement was credited for, in order.
        self.uncredited: list[int] = []
        self.state: Optional[GameState] = None  # the game's state at the last look
        self._soma: Optional[tuple[int, int]] = None   # where Soma was last seen in the room
        self._seen: set[Event] = set()

    def _report(self, new: list[Event], kind: str, target, how: str) -> None:
        event = Event(kind, str(target), how)
        if event not in self._seen:
            self._seen.add(event)
            self.events.append(event)
            new.append(event)

    def look(self) -> list[Event]:
        """Read the game, and return the events that are new since the last look."""
        new: list[Event] = []
        if self.over:
            return new
        target = self.room
        open_pickups = [pickup for pickup in target.pickups if pickup.flag is not None
                        and Event("pickup", str(pickup.number), "collected") not in self._seen]
        reads = list(annotator.STATE_READS)
        if open_pickups:
            reads.append(game.PICKUP_FLAGS)
        blocks = read_many(self.session.emulator, reads)
        state = self.state = annotator.parse_state(blocks[:len(annotator.STATE_READS)])

        if not state.in_game:
            self.over = "gone"
            return new
        if state.room_pointer != target.address:
            door = exit_door(target.exits, self._soma, state.room_pointer) if self._soma else None
            if door is not None:
                self._report(new, "door", door, "left")
            self.over = "left" if door is not None else "gone"
            return new
        if not state.playing:
            return new

        self._soma = state.soma
        if open_pickups:
            flags = blocks[len(annotator.STATE_READS)]
            for pickup in open_pickups:
                if collected(flags, pickup):
                    self._report(new, "pickup", pickup.number, "collected")
        for pickup in target.pickups:
            if not pickup.spawns and touching(state.soma, pickup):
                self._report(new, "pickup", pickup.number, "touched")
        for enemy in self.enemies:
            if reached(state.soma, enemy):
                self._report(new, "enemy", enemy.number, "reached")
        self._defeats(new, state)
        return new

    def _undefeated(self, enemy: Enemy) -> bool:
        return Event("enemy", str(enemy.number), "defeated") not in self._seen

    def _defeats(self, new: list[Event], state: GameState) -> None:
        """Report the defeats that the defeated bits in ``state`` show."""
        struck = sorted({enemy.type_id for enemy in self.enemies
                         if self._undefeated(enemy) and _bit(state.defeated, enemy.type_id)})
        entities = None
        bits = bytearray(state.defeated)
        for type_id in struck:
            group = [enemy for enemy in self.enemies if enemy.type_id == type_id]
            if len(group) == 1:
                self._report(new, "enemy", group[0].number, "defeated")
                continue
            if entities is None:
                table = self.session.emulator.read(*game.ENTITY_TABLE)
                entities = game.parse_entities(table, state.camera)
            # A placement reported at an earlier look counts here too. The game sets the bit
            # again when the placement loses another head or another generated enemy, and that
            # defeat is then explained without a new event.
            down = [enemy for enemy in group if dying(entities, enemy)]
            for enemy in down:
                self._report(new, "enemy", enemy.number, "defeated")
            if not down:
                self.uncredited.append(type_id)
            if any(self._undefeated(enemy) for enemy in group):
                bits[type_id >> 3] &= ~(1 << (type_id & 7))
        if bits != state.defeated:
            self.session.emulator.write(annotator.ENEMY_DEFEATED, bytes(bits))
