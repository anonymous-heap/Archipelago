from __future__ import annotations

from ..._pydantic_compat import BaseModel, parse_obj_as, validator
from .._csv_resources import open_csv
from ..parse_int import parse_hex

__all__ = [
    "PickupInfo",
    "pickup_info_collection",
]

class PickupInfo(BaseModel):
    pickup_number: int
    ptr_address: int
    simple_name: str
    specifier: str | None = None
    flag_offset: int
    item_offset: int
    type_num: int
    type_name: str
    subtype_num: int
    room_identifier: str
    room_address: int
    pickup_number_within_room: int
    x: int
    y: int
    friendly_name: str | None = None

    _parse_hex_addresses = validator("ptr_address", "room_address", pre=True, allow_reuse=True)(parse_hex)

    def _as_tuple(
        self,
    ) -> tuple[
        int,
        int,
        str,
        str | None,
        int,
        int,
        int,
        str,
        int,
        str,
        int,
        int,
        int,
        int,
    ]:
        return (
            self.pickup_number,
            self.ptr_address,
            self.simple_name,
            self.specifier,
            self.flag_offset,
            self.item_offset,
            self.type_num,
            self.type_name,
            self.subtype_num,
            self.room_identifier,
            self.room_address,
            self.pickup_number_within_room,
            self.x,
            self.y,
        )

    def __iter__(self):
        return iter(self._as_tuple())

    def __getitem__(self, index: int):
        return self._as_tuple()[index]

    @property
    def key(self) -> int:
        return self.ptr_address

    @property
    def ptr_hex(self) -> str:
        return hex(self.ptr_address)

    @property
    def room_hex(self) -> str:
        return hex(self.room_address)

    @property
    def identifier_key(self) -> str:
        return f"{self.simple_name}{self.specifier or ''}"

    @property
    def display_name(self) -> str:
        """
        Human-readable AP item/location name. Money (subtype 1) renders its gold value
        (``500 Gold``) instead of the bare number; a duplicate-suffix like ``_a`` becomes `` (A)``.
        ``identifier_key`` stays the internal key; this is display-only (codes/ids are unaffected).\
        """
        spec = (self.specifier or "").lstrip("_")
        if not spec:
            suffix = ""
        elif len(spec) == 1 and spec.isalpha():
            suffix = f" ({spec.upper()})"
        else:
            suffix = f" ({spec})"
        if self.subtype_num == 1:  # money: simple_name is the gold value
            return f"{int(self.simple_name)} Gold{suffix}"
        return f"{self.simple_name}{suffix}"

    @property
    def location_name(self) -> str:
        """
        Human-readable AP *location* name: the friendly room-area name followed by a bracketed
        tag of the room id and the item's ``display_name`` -- e.g. ``White Dragon Hallway [00D
        Potion (A)]``. When no friendly name is mapped it falls back to the bare tag ``00D Potion
        (A)``. This is the location (check) name only; ``display_name`` (the item name) and all
        ids are unaffected.
        """
        tag = f"{self.room_identifier} {self.display_name}"
        return f"{self.friendly_name} [{tag}]" if self.friendly_name else tag

    @property
    def is_hard_mode_only(self) -> bool:
        """
        True for HARD_PICKUP entities (type 5), which the game spawns only in Hard Mode (as
        Soma). They share the same save-flag bitfield as normal pickups, so the only difference
        is whether they exist at all in a given game.
        """
        return self.type_num == 5

    @classmethod
    def find(cls, key: int | str) -> "PickupInfo":
        return find(key)


def _load_csv(filename: str) -> list[dict[str, str]]:
    rows = []
    for row in open_csv(__name__, filename):
        # Skip completely empty lines
        if not any((v or "").strip() for v in row.values()):
            continue
        rows.append(row)
    return rows


def _merge_rows() -> list[PickupInfo]:
    ident_rows = _load_csv("pickup_identifiers.csv")
    room_rows = _load_csv("pickup_rooms.csv")
    friendly_rows = _load_csv("friendly_location_names.csv")

    by_pickup_number_ident = {int(r["pickup_number"]): r for r in ident_rows}
    by_pickup_number_room = {int(r["pickup_number"]): r for r in room_rows}
    by_pickup_number_friendly = {int(r["pickup_number"]): r for r in friendly_rows}

    # The friendly names live in this column.
    _FRIENDLY_COL = "SchwartzGandhi's names"
    if friendly_rows and _FRIENDLY_COL not in friendly_rows[0]:
        raise ValueError(
            f"friendly_location_names.csv is missing the {_FRIENDLY_COL!r} column "
            f"(columns present: {list(friendly_rows[0])})")

    merged: list[dict] = []
    for pickup_number, ident in sorted(by_pickup_number_ident.items()):
        room = by_pickup_number_room.get(pickup_number)
        if room is None:
            raise ValueError(f"pickup_rooms.csv missing entry for pickup_number {pickup_number}")

        ptr_ident = parse_hex(ident["ptr_address"])
        ptr_room = parse_hex(room.get("ptr_address"))
        ptr_address = ptr_ident if ptr_room is None else ptr_room

        if ptr_address is None:
            raise ValueError(f"pickup_number {pickup_number} missing ptr_address in both CSVs")
        if ptr_room is not None and ptr_ident is not None and ptr_room != ptr_ident:
            raise ValueError(
                f"ptr_address mismatch for pickup_number {pickup_number}: "
                f"{ptr_ident} (identifiers) vs {ptr_room} (rooms)"
            )

        # Friendly location name, joined by pickup_number. A missing/blank entry leaves
        # friendly_name None and PickupInfo.location_name falls back to the bare "<room> <item>" tag.
        friendly_row = by_pickup_number_friendly.get(pickup_number)
        friendly_name = (friendly_row.get(_FRIENDLY_COL) or "").strip() if friendly_row else ""
        if friendly_name and friendly_row.get("room_identifier", "").strip() != room["room_identifier"].strip():
            raise ValueError(
                f"friendly_location_names.csv room mismatch for pickup_number {pickup_number}: "
                f"{friendly_row.get('room_identifier')!r} (friendly) vs {room['room_identifier']!r} (rooms)"
            )

        merged.append(
            {
                "pickup_number": pickup_number,
                "ptr_address": ptr_address,
                "simple_name": ident["simple_name"],
                "specifier": ident.get("specifier") or None,
                "flag_offset": int(ident["flag_offset (varA)"]),
                "item_offset": int(ident["item_offset (varB)"]),
                "type_num": int(ident["type_num"]),
                "type_name": ident["type_name"],
                "subtype_num": int(ident["subtype_num"]),
                "room_identifier": room["room_identifier"],
                "room_address": room["room_address"],
                "pickup_number_within_room": int(room["pickup_number_within_room"]),
                "x": int(room["x"]),
                "y": int(room["y"]),
                "friendly_name": friendly_name or None,
            }
        )

    return parse_obj_as(list[PickupInfo], merged)


rows: tuple[PickupInfo, ...] = tuple(_merge_rows())
by_ptr_address: dict[int, PickupInfo] = {row.ptr_address: row for row in rows}
by_pickup_number: dict[int, PickupInfo] = {row.pickup_number: row for row in rows}
by_identifier_key: dict[str, PickupInfo] = {row.identifier_key: row for row in rows}
by_simple_name: dict[str, list[PickupInfo]] = {}
for row in rows:
    by_simple_name.setdefault(row.simple_name, []).append(row)

pickup_info_collection = list(rows)

def find(key: int | str) -> PickupInfo:
    if isinstance(key, int):
        return by_ptr_address.get(key) or by_pickup_number[key]
    if isinstance(key, str) and key.startswith("0x"):
        return by_ptr_address[int(key, 16)]
    if key.isdigit():
        num = int(key)
        return by_ptr_address.get(num) or by_pickup_number[num]
    if key in by_identifier_key:
        return by_identifier_key[key]
    # Fallback: if only one entry shares the simple_name, return it
    matches = by_simple_name.get(key)
    if matches and len(matches) == 1:
        return matches[0]
    raise KeyError(f"No pickup matches key '{key}'")
