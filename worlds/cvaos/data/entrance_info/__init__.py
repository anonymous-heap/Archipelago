from __future__ import annotations

from ..._pydantic_compat import BaseModel, parse_obj_as, validator
from .._csv_resources import open_csv
from ..parse_int import parse_hex

__all__ = [
    "EntranceInfo",
    "rows",
    "ambiguous_door_identifiers",
]

class EntranceInfo(BaseModel):
    """
    Information about a door/entrance in Castlevania: Aria of Sorrow.

    Each entrance represents a transition point between rooms in the game.
    """

    door_number: int
    """Sequential door number in the game data.

    Example: 1, 2, 3, etc.
    """

    door_identifier_nonunique: str
    """Non-unique identifier combining source and destination room identifiers.

    Format: "{room_identifier}:{dest_room_identifier}"
    Example: "000:003" (door from room 000 to room 003)
    """

    door_identifier_unique: str
    """Unique identifier for this door, combining door_identifier
       and, if this is the nth nonunique door_identifier where n > 1, the last 5 hex digits of door_address.
    """

    room_identifier: str
    """Identifier of the source room containing this door.

    Example: "000", "001", "002"
    """

    dest_room_identifier: str
    """Identifier of the destination room this door leads to.

    Example: "003", "002", "005"
    """

    door_address: int
    """Memory address of the door data structure in the game ROM.

    Example: 0x0850EF8C, 0x0850F00C
    """

    room_index: int
    """Index of the source room in the game's room list.

    Example: 0, 1, 2, 3
    """

    room_address: int
    """Memory address of the source room data structure in the game ROM.

    Example: 0x0850EF9C, 0x0850F01C
    """

    door_index_within_room: int
    """Index of this door within its source room's list of doors.

    A room can have multiple doors, each with a sequential index starting at 0.
    Example: 0 (first door in room), 1 (second door), 2 (third door)
    """

    dest_room_address: int
    """Memory address of the destination room data structure in the game ROM.

    Example: 0x0850F15C, 0x0850F0B4
    """

    x_pos_door: int
    """X-coordinate of the door in the source room (in tiles or pixels).

    Example: 2, 1, 255
    """

    y_pos_door: int
    """Y-coordinate of the door in the source room (in tiles or pixels).

    Example: 2, 0, 4, 255
    """

    dest_x_door: int
    """X-coordinate where the player spawns in the destination room (in tiles or pixels).

    Example: 0, 256, 272
    """

    dest_y_door: int
    """Y-coordinate where the player spawns in the destination room (in tiles or pixels).

    Example: 0, 512, 768
    """

    dest_x_offset_door: int
    """X-coordinate offset applied to the spawn position in the destination room.
    """

    dest_y_offset_door: int
    """Y-coordinate offset applied to the spawn position in the destination room.
    """

    _parse_hex_addresses = validator(
        "door_address", "room_address", "dest_room_address", pre=True, allow_reuse=True
    )(parse_hex)

    @property
    def key(self) -> str:
        return self.door_identifier_unique

    @property
    def door_hex(self) -> str:
        return hex(self.door_address)

    @classmethod
    def lookup(cls, key: int | str) -> "EntranceInfo":
        return lookup(key)


def _disambiguation_suffixes(door_id: str, group: list[dict]) -> list[str]:
    """
    Suffix for each row sharing one ``door_identifier``, in the order given.

    A room usually reaches a neighbour through one door, and that door gets no suffix because
    naming it cannot be ambiguous. Where two doors share a room pair, they always sit one map
    cell apart along a single axis, so both are named by that geometry: ``"(upper)"`` and
    ``"(lower)"``, or ``"(left)"`` and ``"(right)"``.

    Both halves are tagged rather than only the second, so that a routing rule naming the bare
    room id is always wrong for such a pair and can be rejected at load. Deriving each name from
    position also matters because ``entrance_info.csv`` is generated from ROM data. Were the
    names assigned by row order, regenerating the file in a different order would silently move
    every routing rule onto the other door.
    """
    if len(group) == 1:
        return [""]
    if len(group) > 2:
        raise ValueError(
            f"door_identifier {door_id!r} has {len(group)} doors; only one or two are supported, "
            f"because the (lower)/(right) naming assumes a pair")

    first, second = group
    if first["y_pos_door"] != second["y_pos_door"]:
        first_is_low = int(first["y_pos_door"]) < int(second["y_pos_door"])
        low, high = " (upper)", " (lower)"
    elif first["x_pos_door"] != second["x_pos_door"]:
        first_is_low = int(first["x_pos_door"]) < int(second["x_pos_door"])
        low, high = " (left)", " (right)"
    else:
        raise ValueError(
            f"door_identifier {door_id!r} has two doors at the same position "
            f"({first['x_pos_door']}, {first['y_pos_door']}); they cannot be told apart")

    return [low, high] if first_is_low else [high, low]


def _load() -> tuple[EntranceInfo, ...]:
    reader = open_csv(__name__, "entrance_info.csv")
    cleaned = [
        row
        for row in reader
        if any((v or "").strip() for v in row.values())
    ]

    by_door_id: dict[str, list[dict]] = {}
    for row in cleaned:
        by_door_id.setdefault(row["door_identifier"], []).append(row)

    for door_id, group in by_door_id.items():
        for row, suffix in zip(group, _disambiguation_suffixes(door_id, group)):
            row["door_identifier_nonunique"] = door_id
            row["door_identifier_unique"] = f"{door_id}{suffix}"

    for row in cleaned:
        del row["door_identifier"]

    return tuple(parse_obj_as(list[EntranceInfo], cleaned))


rows: tuple[EntranceInfo, ...] = _load()

# Bare identifiers served by more than one door. A routing rule naming one of these has not
# said which door it means, so the routing loaders reject it instead of guessing.
ambiguous_door_identifiers: frozenset[str] = frozenset(
    row.door_identifier_nonunique for row in rows
    if row.door_identifier_unique != row.door_identifier_nonunique
)
by_door_number: dict[int, EntranceInfo] = {row.door_number: row for row in rows}
by_door_identifier_unique: dict[str, EntranceInfo] = {row.door_identifier_unique: row for row in rows}
by_door_address: dict[int, EntranceInfo] = {row.door_address: row for row in rows}


def lookup(key: int | str) -> EntranceInfo:
    if isinstance(key, int):
        return by_door_number.get(key) or by_door_address[key]
    if isinstance(key, str) and key.startswith("0x"):
        return by_door_address[int(key, 16)]
    return by_door_identifier_unique[key]

