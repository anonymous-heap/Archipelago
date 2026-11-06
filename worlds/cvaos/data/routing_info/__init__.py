"""
Routing requirements: what the player needs to cross rooms and to reach pickups and enemies.

``rows`` holds the requirements for crossing a room from one of its doors to another, and
``transdoor_connection_rows`` pairs the two sides of each door. ``pickup_region_rows`` and
``enemy_region_rows`` hold the requirements for reaching a pickup or an enemy from a door of its
room. ``enemy_placements`` lists every enemy placement in the enemy CSV, whether or not it has
routing.

Each row keeps its CSV ability columns and combo cells, and ``get_requirement_bitmasks`` turns
them into :class:`AbilityCombo` masks.

Where two doors connect the same pair of rooms, a door reference must carry the door's position
tag. An untagged reference raises ``ValueError`` at import, except in the override file, whose
rows are not checked.
"""

from __future__ import annotations

import logging
import re
from enum import IntFlag, auto
from typing import Iterable, NamedTuple

from ..._pydantic_compat import BaseModel
from .._csv_resources import open_csv, open_csv_if_exists
from ..entrance_info import ambiguous_door_identifiers

__all__ = [
    "AbilityCombo",
    "RoutingInfo",
    "rows",
    "EntranceToPickupRegionInfo",
    "pickup_region_rows",
    "lookup_pickup_region_requirement",
    "EntranceToEnemyRegionInfo",
    "enemy_region_rows",
    "EnemyPlacement",
    "enemy_placements",
    "by_enemy_name_for_enemy_regions",
    "by_enemy_number_for_enemy_regions",
    "enemy_meta_by_number",
    "resolve_enemy_number",
    "TransdoorConnection",
    "transdoor_connection_rows",
    "by_from_entrance_for_transdoor",
]


def _reject_ambiguous(node: str, where: str) -> str:
    """
    Return ``node`` unchanged, or raise if it names a room pair served by two doors.

    Where two doors connect the same pair of rooms, each identifier carries a position tag such
    as ``(left)``, so ``"500:501"`` on its own does not say which door is meant. Guessing would
    bind every such rule to one of the doors and leave the other unreachable, so an untagged
    reference fails at load instead.
    """
    if node in ambiguous_door_identifiers:
        raise ValueError(
            f"{where}: {node!r} names two different doors. Say which one, for example "
            f"{node!r} with '(upper)', '(lower)', '(left)', or '(right)' appended.")
    return node


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"true", "1", "yes", "y"}


def _canonical_ability_name(name: str) -> str:
    # E.g. make "Panth." (in CSV) just "Panth".
    return name.strip().rstrip(".")


class AbilityCombo(IntFlag):
    """
    Bit flags for the abilities and conditions that routing requirements name.

    A requirement mask ORs flags together, and it is met only when every flag in it is met. Most
    flags stand for a soul's ability. The rest are movement techniques, room conditions, and
    ``Impossible``, which is never met.
    """

    None_ = 0

    Glide = auto()   # Flying Armor
    """Flying Armor"""
    Slide = auto()   # Skeleton Blaze
    """Skeleton Blaze"""
    DJump = auto()   # Malphas
    """Malphas"""
    HJump = auto()   # Hippogryph
    """Hippogryph"""
    WWalk = auto()   # Undine
    """Undine"""
    Dive = auto()    # Skula
    """Skula"""
    Panth = auto()   # Black Panther
    """Black Panther"""
    Bat = auto()     # Giant Bat
    """Giant Bat"""
    Impossible = auto()
    """Impossible requirement (never satisfied)"""
    BDash = auto()   # Grave Keeper
    """Grave Keeper"""
    Kick = auto()    # Kicker Skeleton
    """Kicker Skeleton"""
    Tform = auto()   # Devil/Curly/Manticore
    """Devil/Curly/Manticore"""

    Enemy = auto()   # Presence of a kickable enemy
    """Presence of a kickable enemy"""
    PixPer = auto()  # Pixel-perfect platforming
    """Pixel-perfect platforming"""
    Clip = auto()    # Platform Clip
    """Platform Clip"""
    Floor = auto()   # Floor reachable and hittable
    """Floor reachable and hittable"""
    Ceil = auto()    # Ceiling reachable and hittable
    """Ceiling reachable and hittable"""
    Vert = auto()    # Vertical room entrance
    """Vertical room entrance"""
    QSave = auto()   # Quick Save entrance reset
    """Quick Save entrance reset"""

    # Souls that gate routes as items. New flags go at the end so that the bit values above never
    # change. regions.py maps each soul to its item through _ABILITY_TO_ITEM. Galamoth is a ground
    # pickup, but Chronomage has no pickup, so its item is never placed and a requirement that
    # includes it is never met.
    Galamoth = auto()
    """Galamoth soul"""
    Chronomage = auto()
    """Chronomage soul (it has no pickup, so a requirement that includes it is never met)"""

    # Appended after the souls for the same reason: earlier bit values stay as they are.
    InZip = auto()   # Inbounds zip
    """Inbounds zip"""

# Canonical enum-key -> bitflag
_ABILITY_BY_NAME: dict[str, AbilityCombo] = {
    "None": AbilityCombo.None_,
    "Glide": AbilityCombo.Glide,
    "Slide": AbilityCombo.Slide,
    "DJump": AbilityCombo.DJump,
    "HJump": AbilityCombo.HJump,
    "WWalk": AbilityCombo.WWalk,
    "Dive": AbilityCombo.Dive,
    "Panth": AbilityCombo.Panth,
    "Bat": AbilityCombo.Bat,
    "Impossible": AbilityCombo.Impossible,
    "BDash": AbilityCombo.BDash,
    "Kick": AbilityCombo.Kick,
    "Tform": AbilityCombo.Tform,
    "Enemy": AbilityCombo.Enemy,
    "PixPer": AbilityCombo.PixPer,
    "Clip": AbilityCombo.Clip,
    "Floor": AbilityCombo.Floor,
    "Ceil": AbilityCombo.Ceil,
    "Vert": AbilityCombo.Vert,
    "QSave": AbilityCombo.QSave,
    "Galamoth": AbilityCombo.Galamoth,
    # Chronomage has no pickup, so a requirement that includes it is never met.
    "Chronomage": AbilityCombo.Chronomage,
    "InZip": AbilityCombo.InZip,
}


def _minimize_req_masks(reqs: Iterable[int]) -> tuple[int, ...]:
    """
    Return the masks in ``reqs`` that no other mask makes redundant.

    A mask is dropped when another mask is a subset of it, because meeting the larger mask always
    meets the smaller one too. The result is ordered by bit count, then by value.
    """
    uniq = sorted(set(int(r) for r in reqs), key=lambda m: (m.bit_count(), m))
    out: list[int] = []
    for r in uniq:
        # dominated by something already kept? (exists o ⊆ r)
        if any((o | r) == r for o in out):
            continue

        # remove anything dominated by r (remove o where r ⊆ o)
        out = [o for o in out if not ((r | o) == o)]
        out.append(r)
    return tuple(out)


_PAREN_RE = re.compile(r"\(([^)]+)\)")


def _parse_combo_text_to_mask(text: str, *, strict: bool) -> int | None:
    """
    Return the mask for one ``Misc. combo`` cell, or None when the cell sets no flag.

    Only the flag names inside parentheses count, so ``"Malphas (DJump), Flying Armor (Glide)"``
    gives ``DJump | Glide``. An unknown name raises when ``strict`` is set and is skipped
    otherwise.
    """
    if not text or not text.strip():
        return None

    mask = 0
    for inside in _PAREN_RE.findall(text):
        key = inside.strip()
        flag = _ABILITY_BY_NAME.get(key)
        if flag is None:
            if strict:
                raise ValueError(f"Unknown combo token: ({key}) in {text!r}")
            continue
        if flag != AbilityCombo.None_:
            mask |= int(flag)

    return mask or None


def _ability_and_combo_headers(fieldnames: Iterable[str]) -> tuple[list[str], list[str]]:
    names = list(fieldnames or [])
    try:
        start = names.index("None")
        end = None
        for i, name in enumerate(names[start:], start):
            if name.strip().startswith("Misc. combo") or name.strip() == "<alt 2>":
                end = i
                break
        if end is None:
            end = len(names)
        ability_headers = names[start:end]
    except ValueError:
        ability_headers = []
    combo_headers = [h for h in names if h.strip().startswith("Misc. combo")]
    return ability_headers, combo_headers


def _extract_abilities_and_combo_texts(
    row: dict,
    ability_headers: Iterable[str],
    combo_headers: Iterable[str],
) -> tuple[frozenset[str], tuple[str, ...]]:
    abilities = frozenset(
        _canonical_ability_name(header)
        for header in ability_headers
        if _truthy(row.get(header))
    )
    combo_texts = tuple(
        (row.get(h) or "").strip()
        for h in combo_headers
        if (row.get(h) or "").strip()
    )
    return abilities, combo_texts


class TransdoorConnection(BaseModel):
    """
    Maps one side of a physical door to the entrance node on the other side.

    :param from_entrance: Entrance node on one side of the door.
    :param to_entrance: Entrance node on the opposite side of the same door.
    """

    class Config:
        frozen = True

    from_entrance: str
    to_entrance: str


def _load_transdoor_connections() -> tuple[TransdoorConnection, ...]:
    out: list[TransdoorConnection] = []
    for row in open_csv(__name__, "default_transdoor_entrance_connections.csv"):
        if not any((v or "").strip() for v in row.values()):
            continue
        out.append(TransdoorConnection(
            from_entrance=_reject_ambiguous(row["from_entrance"].strip(),
                                            "default_transdoor_entrance_connections.csv"),
            to_entrance=_reject_ambiguous(row["to_entrance"].strip(),
                                          "default_transdoor_entrance_connections.csv"),
        ))

    # Apply the overrides: a row with does_exist=FALSE removes that crossing, and a row with
    # is_override=TRUE adds one. A row that does neither restates a crossing and has no effect.
    override_file = "override_transdoor_entrance_connections.csv"
    override_reader = open_csv_if_exists(__name__, override_file)
    if override_reader is not None:
        removals: set[tuple[str, str]] = set()
        additions: list[TransdoorConnection] = []
        for row in override_reader:
            if not any((v or "").strip() for v in row.values()):
                continue
            from_e = row["from_entrance"].strip()
            to_e = row["to_entrance"].strip()
            removes = not _truthy(row.get("does_exist"))
            adds = _truthy(row.get("is_override"))
            if removes or adds:
                # A row that changes the table has to name each door exactly. An untagged name
                # for a room pair with two doors would match no crossing and do nothing.
                _reject_ambiguous(from_e, override_file)
                _reject_ambiguous(to_e, override_file)
            if removes:
                removals.add((from_e, to_e))
            if adds:
                additions.append(TransdoorConnection(from_entrance=from_e, to_entrance=to_e))
        unmatched = removals - {(c.from_entrance, c.to_entrance) for c in out}
        if unmatched:
            raise ValueError(f"{override_file}: these removals match no crossing in "
                             f"default_transdoor_entrance_connections.csv: {sorted(unmatched)}")
        out = [c for c in out if (c.from_entrance, c.to_entrance) not in removals]
        out.extend(additions)

    return tuple(out)


def _entrance_identifiers_from_cell(room_id: str, cell: str | None) -> set[str]:
    """
    Return the entrance nodes in ``room_id`` that a ``dest_room_identifier`` cell names.

    A cell of ``Any`` names every node in ``_arrivals_by_room`` for the room. Each of those is
    the door on ``room_id``'s own side of a crossing, so it sits behind any gate on the crossing.

    Otherwise the cell lists neighbor rooms separated by commas, and each one names the node
    ``f"{room_id}:{neighbor}"``. A listed node is kept only if it is a ``from_entrance`` in
    ``by_from_entrance_for_transdoor``, so removing a door from
    default_transdoor_entrance_connections.csv also removes it here. A blank cell names nothing.
    """
    if cell is None:
        return set()

    text = cell.strip()
    if not text:
        return set()

    if text.lower() == "any":
        return set(_arrivals_by_room.get(room_id, set()))

    identifiers: set[str] = set()
    for neighbor in (part.strip() for part in text.split(",") if part.strip()):
        entrance = _reject_ambiguous(f"{room_id}:{neighbor}", "dest_room_identifier")
        if entrance in by_from_entrance_for_transdoor:
            identifiers.add(entrance)
    return identifiers


def _requirement_bitmasks_from(
    abilities: Iterable[str],
    combo_texts: Iterable[str],
    *,
    include_combos: bool = True,
    minimize: bool = True,
    strict_combo_tokens: bool = True,
) -> tuple[int, ...]:
    masks: list[int] = []

    for ability in abilities:
        if ability == "None":
            masks.append(0)
            continue
        flag = _ABILITY_BY_NAME.get(ability)
        if flag is None:
            if strict_combo_tokens:
                raise ValueError(f"Unknown ability column/token: {ability!r}")
            continue
        if flag != AbilityCombo.None_:
            masks.append(int(flag))

    if include_combos:
        for text in combo_texts:
            m = _parse_combo_text_to_mask(text, strict=strict_combo_tokens)
            if m is not None:
                masks.append(m)

    if not masks:
        return tuple()

    return _minimize_req_masks(masks) if minimize else tuple(int(m) for m in masks)


class RoutingInfo(BaseModel):
    """
    Requirements for crossing one room from one of its doors to another.

    Each instance is one row of ``entrance_to_entrance_requirements.csv``, and it covers one
    direction only. The doors are named by the rooms they lead to: the route starts at the door
    to ``from_room`` and ends at the door to ``to_room``, both inside ``room_id``. Where two doors
    of ``room_id`` lead to the same room, the name also carries the door's position tag, as in
    ``"500 (left)"``.
    """

    class Config:
        extra = "allow"
        frozen = True

    connection_number: int
    room_id: str
    from_room: str
    to_room: str
    variant: int | None

    # Base TRUE/FALSE ability columns (treated as singleton requirement options)
    abilities: frozenset[str]

    # Raw text from Misc. combo N columns
    combo_texts: tuple[str, ...] = ()

    @classmethod
    def from_row(
        cls,
        row: dict,
        ability_headers: Iterable[str],
        combo_headers: Iterable[str],
    ) -> "RoutingInfo":
        abilities, combo_texts = _extract_abilities_and_combo_texts(
            row, ability_headers, combo_headers
        )

        ignore_keys = {
            "entrance_connection_number",
            "RoomID",
            "From",
            "To",
            "",
            *ability_headers,
            *combo_headers,
        }

        room_id = row["RoomID"]
        where = f"entrance_to_entrance_requirements.csv row {row['entrance_connection_number']}"
        _reject_ambiguous(f"{room_id}:{row['From']}", where)
        _reject_ambiguous(f"{room_id}:{row['To']}", where)

        return cls(
            connection_number=int(row["entrance_connection_number"]),
            room_id=room_id,
            from_room=row["From"],
            to_room=row["To"],
            variant=int(row[""]) if row.get("") else None,
            abilities=abilities,
            combo_texts=combo_texts,
            **{k: v for k, v in row.items() if k not in ignore_keys},
        )

    def requires(self, ability: str) -> bool:
        return _canonical_ability_name(ability) in self.abilities

    @property
    def key(self) -> int:
        return self.connection_number

    @classmethod
    def lookup(cls, key: int | tuple[str, str]) -> "RoutingInfo":
        return lookup(key)

    def get_requirement_bitmasks(
        self,
        *,
        include_combos: bool = True,
        minimize: bool = True,
        strict_combo_tokens: bool = True,
    ) -> tuple[int, ...]:
        """
        Return the requirement masks for this crossing, any one of which is enough.

        A ``TRUE`` ability column gives a mask holding that one flag, and a ``TRUE`` in the
        ``None`` column gives the empty mask ``0``. Each ``Misc. combo`` cell gives one mask
        holding every flag it names.
        """
        return _requirement_bitmasks_from(
            self.abilities,
            self.combo_texts,
            include_combos=include_combos,
            minimize=minimize,
            strict_combo_tokens=strict_combo_tokens,
        )


def _load() -> tuple[RoutingInfo, ...]:
    reader = open_csv(__name__, "entrance_to_entrance_requirements.csv")
    ability_headers, combo_headers = _ability_and_combo_headers(reader.fieldnames or [])

    out: list[RoutingInfo] = []
    for row in reader:
        if not any((v or "").strip() for v in row.values()):
            continue
        out.append(RoutingInfo.from_row(row, ability_headers, combo_headers))

    return tuple(out)


rows: tuple[RoutingInfo, ...] = _load()
by_connection_number: dict[int, RoutingInfo] = {r.connection_number: r for r in rows}
by_from_to: dict[tuple[str, str], RoutingInfo] = {(r.from_room, r.to_room): r for r in rows}

def lookup(key: int | tuple[str, str]) -> RoutingInfo:
    if isinstance(key, int):
        return by_connection_number[key]
    return by_from_to[key]


class EntranceToPickupRegionInfo(BaseModel):
    """
    Requirements for reaching a pickup region from a specific entrance.

    These requirements are symmetric: they apply both to reaching the pickup region
    and returning from it.
    """

    class Config:
        extra = "allow"
        frozen = True

    pickup_number: int
    pickup_room: str
    item_name: str
    variant: int | None
    entrance_identifier: str

    abilities: frozenset[str]
    combo_texts: tuple[str, ...] = ()

    @classmethod
    def from_row(
        cls,
        row: dict,
        ability_headers: Iterable[str],
        combo_headers: Iterable[str],
        entrance_identifier: str,
    ) -> "EntranceToPickupRegionInfo":
        abilities, combo_texts = _extract_abilities_and_combo_texts(
            row, ability_headers, combo_headers
        )

        return cls(
            pickup_number=int(row["pickup_number"]),
            pickup_room=row["Room"],
            item_name=row["Item Name"],
            variant=int(row[""]) if row.get("") else None,
            entrance_identifier=entrance_identifier,
            abilities=abilities,
            combo_texts=combo_texts,
            **{k: v for k, v in row.items() if k not in {"pickup_number", "Room", "Item Name", "", "dest_room_identifier", *ability_headers, *combo_headers}},
        )

    @property
    def key(self) -> tuple[str, int]:
        return (self.entrance_identifier, self.pickup_number)

    @property
    def pickup_region_identifier(self) -> int:
        return self.pickup_number

    def get_requirement_bitmasks(
        self,
        *,
        include_combos: bool = True,
        minimize: bool = True,
        strict_combo_tokens: bool = True,
    ) -> tuple[int, ...]:
        return _requirement_bitmasks_from(
            self.abilities,
            self.combo_texts,
            include_combos=include_combos,
            minimize=minimize,
            strict_combo_tokens=strict_combo_tokens,
        )


def _load_pickup_region_requirements() -> tuple[EntranceToPickupRegionInfo, ...]:
    reader = open_csv(__name__, "symmetric_entrance_to_pickup_region_requirements.csv")
    ability_headers, combo_headers = _ability_and_combo_headers(reader.fieldnames or [])

    seen_keys: set[tuple[str, int]] = set()
    out: list[EntranceToPickupRegionInfo] = []
    for row in reader:
        if not any((v or "").strip() for v in row.values()):
            continue

        room_id = row.get("Room", "").strip()
        entrances = _entrance_identifiers_from_cell(room_id, row.get("dest_room_identifier"))
        if not entrances:
            raise ValueError(f"No entrance identifiers resolved for pickup_number {row.get('pickup_number')} in room {room_id!r}")

        for entrance_identifier in sorted(entrances):
            key = (entrance_identifier, int(row["pickup_number"]))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(
                EntranceToPickupRegionInfo.from_row(
                    row, ability_headers, combo_headers, entrance_identifier
                )
            )

    return tuple(out)


# Transdoor connections must be loaded before pickup region requirements, because
# _entrance_identifiers_from_cell (called during pickup region loading) relies on
# by_from_entrance_for_transdoor and _arrivals_by_room.
transdoor_connection_rows: tuple[TransdoorConnection, ...] = _load_transdoor_connections()
by_from_entrance_for_transdoor: dict[str, TransdoorConnection] = {
    r.from_entrance: r for r in transdoor_connection_rows
}
_arrivals_by_room: dict[str, set[str]] = {}
for _r in transdoor_connection_rows:
    # The arrival node is the to_entrance: the door on the side the player ends up on, inside the
    # arrival room. Using from_entrance instead would attach pickups and enemies to the neighbor's
    # side of the door, in front of any gate on the crossing.
    _arrival_room, _ = _r.to_entrance.split(":", 1)
    _arrivals_by_room.setdefault(_arrival_room, set()).add(_r.to_entrance)

pickup_region_rows: tuple[EntranceToPickupRegionInfo, ...] = _load_pickup_region_requirements()
by_pickup_number_for_pickup_regions: dict[int, list[EntranceToPickupRegionInfo]] = {}
by_entrance_identifier_for_pickup_regions: dict[str, list[EntranceToPickupRegionInfo]] = {}
by_entrance_and_pickup: dict[tuple[str, int], EntranceToPickupRegionInfo] = {}

for row in pickup_region_rows:
    by_pickup_number_for_pickup_regions.setdefault(row.pickup_number, []).append(row)
    by_entrance_identifier_for_pickup_regions.setdefault(row.entrance_identifier, []).append(row)
    by_entrance_and_pickup[(row.entrance_identifier, row.pickup_number)] = row



def lookup_pickup_region_requirement(entrance_identifier: str, pickup_number: int) -> EntranceToPickupRegionInfo:
    return by_entrance_and_pickup[(entrance_identifier, pickup_number)]


class EntranceToEnemyRegionInfo(BaseModel):
    """
    Requirements for reaching one enemy placement from a specific entrance.

    This is the enemy counterpart of :class:`EntranceToPickupRegionInfo`. Logic uses these rows
    to reach the enemies whose souls it relies on, such as Flame Demon and Succubus, since those
    souls are not ground pickups and never enter the item pool. Enemy checks also use them to
    decide when a placement can be reached. The requirements are symmetric, so they apply both to
    reaching the enemy and to returning from it.

    Each placement has its own ``enemy_number``. ``enemy_name`` plus ``specifier`` is unique only
    *within a room* (``Succubus_a`` is in both room 90D and room 90E), so use ``enemy_number`` to
    identify a single placement.
    """

    class Config:
        extra = "allow"
        frozen = True

    enemy_number: int
    enemy_room: str
    enemy_name: str
    specifier: str
    entrance_identifier: str

    abilities: frozenset[str]
    combo_texts: tuple[str, ...] = ()

    @classmethod
    def from_row(
        cls,
        row: dict,
        ability_headers: Iterable[str],
        combo_headers: Iterable[str],
        entrance_identifier: str,
    ) -> "EntranceToEnemyRegionInfo":
        abilities, combo_texts = _extract_abilities_and_combo_texts(
            row, ability_headers, combo_headers
        )
        return cls(
            enemy_number=int(row["enemy_number"]),
            enemy_room=(row.get("room_id") or "").strip(),
            enemy_name=(row.get("Enemy Name") or "").strip(),
            specifier=(row.get("Specifier") or "").strip(),
            entrance_identifier=entrance_identifier,
            abilities=abilities,
            combo_texts=combo_texts,
        )

    @property
    def key(self) -> tuple[str, int]:
        return (self.entrance_identifier, self.enemy_number)

    @property
    def identifier_key(self) -> str:
        """Human-readable enemy id, e.g. "Succubus_a". NOT globally unique across rooms."""
        return f"{self.enemy_name}{self.specifier}"

    def get_requirement_bitmasks(
        self,
        *,
        include_combos: bool = True,
        minimize: bool = True,
        strict_combo_tokens: bool = True,
    ) -> tuple[int, ...]:
        return _requirement_bitmasks_from(
            self.abilities,
            self.combo_texts,
            include_combos=include_combos,
            minimize=minimize,
            strict_combo_tokens=strict_combo_tokens,
        )


def _load_enemy_region_requirements() -> tuple[EntranceToEnemyRegionInfo, ...]:
    skipped_unresolved = 0
    reader = open_csv(__name__, "symmetric_entrance_to_enemy_region_requirements.csv")
    ability_headers, combo_headers = _ability_and_combo_headers(reader.fieldnames or [])

    seen_keys: set[tuple[str, int]] = set()
    out: list[EntranceToEnemyRegionInfo] = []
    for row in reader:
        if not any((v or "").strip() for v in row.values()):
            continue

        room_id = (row.get("room_id") or "").strip()
        entrances = _entrance_identifiers_from_cell(room_id, row.get("dest_room_identifier"))
        if not entrances:
            # Unlike pickups (which raise), enemy rows are bulk data; a blank or
            # unresolved dest_room_identifier just means we can't place this instance
            # in the region graph, so skip it.
            skipped_unresolved += 1
            continue

        for entrance_identifier in sorted(entrances):
            key = (entrance_identifier, int(row["enemy_number"]))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(
                EntranceToEnemyRegionInfo.from_row(
                    row, ability_headers, combo_headers, entrance_identifier
                )
            )

    if skipped_unresolved:
        logging.info(
            "cvaos: skipped %d enemy routing rows with unresolved dest_room_identifier",
            skipped_unresolved,
        )
    return tuple(out)


enemy_region_rows: tuple[EntranceToEnemyRegionInfo, ...] = _load_enemy_region_requirements()

# enemy_number -> its per-entrance routing rows
by_enemy_number_for_enemy_regions: dict[int, list[EntranceToEnemyRegionInfo]] = {}
# enemy_name -> set of enemy_numbers of that type (drives "reach ANY instance")
by_enemy_name_for_enemy_regions: dict[str, set[int]] = {}
# enemy_number -> (enemy_name, specifier, room) for region naming / reverse lookup
enemy_meta_by_number: dict[int, tuple[str, str, str]] = {}
# (room, enemy_name, specifier) -> enemy_number (unique within a room)
_enemy_number_by_room_name_spec: dict[tuple[str, str, str], int] = {}

for _enemy_row in enemy_region_rows:
    by_enemy_number_for_enemy_regions.setdefault(_enemy_row.enemy_number, []).append(_enemy_row)
    by_enemy_name_for_enemy_regions.setdefault(_enemy_row.enemy_name, set()).add(_enemy_row.enemy_number)
    enemy_meta_by_number[_enemy_row.enemy_number] = (
        _enemy_row.enemy_name, _enemy_row.specifier, _enemy_row.enemy_room,
    )
    _enemy_number_by_room_name_spec[
        (_enemy_row.enemy_room, _enemy_row.enemy_name, _enemy_row.specifier)
    ] = _enemy_row.enemy_number


class EnemyPlacement(NamedTuple):
    """One enemy in one room, as the enemy routing CSV lists it, with or without routing."""

    enemy_number: int  # unique per placement, which can span several CSV rows
    room: str
    name: str
    specifier: str
    x: int  # position in the room, in pixels
    y: int


def _load_enemy_placements() -> tuple[EnemyPlacement, ...]:
    """Return every enemy placement in the enemy routing CSV.

    Unlike :data:`enemy_region_rows`, this keeps the placements whose ``dest_room_identifier`` is
    blank, which are most of them. Enemy checks need every placement, and their logic applies a
    fallback rule to those that have no routing yet. A placement routed from several entrances can
    span several CSV rows, but it is listed here once.
    """
    placements = []
    seen: set[int] = set()
    for row in open_csv(__name__, "symmetric_entrance_to_enemy_region_requirements.csv"):
        name = (row.get("Enemy Name") or "").strip()
        if not name or int(row["enemy_number"]) in seen:
            continue
        seen.add(int(row["enemy_number"]))
        placements.append(EnemyPlacement(int(row["enemy_number"]), (row.get("room_id") or "").strip(),
                                         name, (row.get("Specifier") or "").strip(),
                                         int(row.get("x") or 0), int(row.get("-y") or 0)))
    return tuple(placements)


enemy_placements: tuple[EnemyPlacement, ...] = _load_enemy_placements()


def resolve_enemy_number(room_id: str, enemy_name: str, specifier: str = "") -> int:
    """Map the human form ``(room, name, specifier)`` to the unique ``enemy_number``."""
    return _enemy_number_by_room_name_spec[(room_id, enemy_name, specifier)]
