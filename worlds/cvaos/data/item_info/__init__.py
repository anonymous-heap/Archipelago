from __future__ import annotations

from ..._pydantic_compat import BaseModel, parse_obj_as
from .._csv_resources import open_csv, open_csv_if_exists

__all__ = [
    "ItemInfo",
    "rows",
]


class ItemInfo(BaseModel):
    item_category: str
    id: int
    name: str
    item_number: int
    progression: bool = False
    useful: bool = False
    filler: bool = False
    breaks_ceilings: bool = False
    breaks_floors: bool = False

    def _as_tuple(self) -> tuple[str, int, str, int, bool, bool, bool, bool, bool]:
        return (
            self.item_category,
            self.id,
            self.name,
            self.item_number,
            self.progression,
            self.useful,
            self.filler,
            self.breaks_ceilings,
            self.breaks_floors,
        )

    def __iter__(self):
        return iter(self._as_tuple())

    def __getitem__(self, index: int):
        return self._as_tuple()[index]

    @property
    def key(self) -> str:
        return self.name

    @classmethod
    def find(cls, key: str | tuple[str, int]) -> "ItemInfo":
        """Convenience passthrough to module-level lookup."""

        return find(key)


def _parse_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"true", "1", "yes", "y"}


def _load() -> list[ItemInfo]:
    importance_rows = {
        row["name"]: row
        for row in open_csv(__name__, "item_importance.csv")
        if any((v or "").strip() for v in row.values())
    }

    extended_reader = open_csv_if_exists(__name__, "item_info_extended.csv")
    extended_rows = {
        row["name"]: row
        for row in (extended_reader or ())
        if any((v or "").strip() for v in row.values())
    }

    merged: list[dict] = []
    for row in open_csv(__name__, "item_info.csv"):
        if not any((v or "").strip() for v in row.values()):
            continue

        importance = importance_rows.get(row["name"])
        if importance is None:
            raise ValueError(f"item_importance.csv missing entry for item '{row['name']}'")

        extended = extended_rows.get(row["name"])
        merged.append(
            {
                "item_category": row["item_category"],
                "id": int(row["id"]),
                "name": row["name"],
                "item_number": int(importance["item_number"]),
                "progression": _parse_bool(importance.get("progression")),
                "useful": _parse_bool(importance.get("useful")),
                "filler": _parse_bool(importance.get("filler")),
                "breaks_ceilings": _parse_bool(extended.get("breaks_ceilings") if extended else None),
                "breaks_floors": _parse_bool(extended.get("breaks_floors") if extended else None),
            }
        )

    return parse_obj_as(list[ItemInfo], merged)


rows: tuple[ItemInfo, ...] = tuple(_load())
by_name: dict[str, ItemInfo] = {row.name: row for row in rows}
by_category_id: dict[tuple[str, int], ItemInfo] = {
    (row.item_category, row.id): row for row in rows
}
by_item_number: dict[int, ItemInfo] = {row.item_number: row for row in rows}

def find(key: str | tuple[str, int] | int) -> ItemInfo:
    """Look up by name, (item_category, id), or item_number."""

    if isinstance(key, tuple):
        return by_category_id[key]
    if isinstance(key, int):
        return by_item_number[key]
    return by_name[key]
