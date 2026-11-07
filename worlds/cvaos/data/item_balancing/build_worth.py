"""
Builds ``worth.csv``: how much each item is worth, from 0 to 1, on the scale of a vanilla run.

An item's worth is the point in a vanilla run where the castle's own pickups already offer as
much. Weapons, armor and restoratives get it from ladders, and the other items from their
tier-list desirability.

Each ladder is made of the pickups of one class, ordered by the depth where the vanilla route
first reaches them. A pickup joins the ladder only when it beats every pickup of its class that
comes earlier, so the ladder's utility rises with depth. An item between two rungs gets a depth
interpolated between them. Below the first rung, depth falls in proportion to utility, down to 0.

* A weapon takes its depth from the ladder of its own class, ranked by ATK. A weapon outside its
  class's ladder, or in a class with no pickup, uses the ladder of all weapons instead. A weapon
  above the top of its class's ladder is worth at least that top rung's depth.
* Armor is one class, ranked by DEF.
* HP and MP restoratives are two classes, ranked by the logarithm of the amount they restore.
* Weapons and armor take their desirability instead when it is higher, which counts effects that
  ATK and DEF miss.
* Accessories, cures and the other items have no ladder, so they take their desirability. An
  unrated accessory is worth 0.2, and any other unrated item 0.1.
* Gold is worth 0.02.
* Valmanway, Chaos Ring and Soul Eater Ring are set to 1.0, so that no rule looking for an item of
  similar worth ever picks one of them.

The inputs are ``worth_inputs.csv`` (each item's class, utility and earliest vanilla pickup
depth; the README says where it came from), ``desirability.csv``, and the item and pickup tables.
Run from anywhere: ``python build_worth.py``.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

HERE = Path(__file__).resolve().parent
INPUTS_CSV = HERE / "worth_inputs.csv"
DESIRABILITY_CSV = HERE / "desirability.csv"
ITEM_INFO_CSV = HERE.parent / "item_info" / "item_info.csv"
PICKUPS_CSV = HERE.parent / "pickup_info" / "pickup_identifiers.csv"
OUT_CSV = HERE / "worth.csv"

WEAPON_CLASSES = ("dagger", "sword", "whip", "great_sword", "katana", "axe_hammer", "rapier",
                  "spear", "fist", "gun", "special")
LADDER_CLASSES = WEAPON_CLASSES + ("armor", "hp", "mp")
LOG_SCALE_CLASSES = ("hp", "mp")
# A pickup weaker than this is not a rung, because its ATK does not describe its use. The Bamboo
# Sword (ATK 1) breaks ceilings, and the Handgun's ATK of 1 is not its shot damage.
MIN_RUNG_UTILITY = 5

GOLD_WORTH = 0.02
UNRATED_ACCESSORY_WORTH = 0.2
UNRATED_OTHER_WORTH = 0.1
FIXED_WORTH = {"Valmanway": 1.0, "Chaos Ring": 1.0, "Soul Eater Ring": 1.0}
MONEY_SUBTYPE = 1

# worth.csv keeps this many decimals. build() checks that rounding moves no item across a line
# that the design compares worth with, nor any pair of items across the 0.05 replacement window.
DECIMALS = 6
LINES = (0.25, 0.45, 0.6)
WINDOW = 0.05

Rung = Tuple[float, float, str]  # (utility, depth, item)


class Item(NamedTuple):
    name: str
    category: str
    item_class: str
    utility: float
    earliest_pickup_depth: Optional[float]
    desirability: Optional[float]


def _read(path: Path) -> List[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def load_items() -> List[Item]:
    """Return every item of item_info.csv except souls, in item number order."""
    inputs = {row["name"]: row for row in _read(INPUTS_CSV)}
    desirability = {row["name"]: float(row["desirability"]) for row in _read(DESIRABILITY_CSV)}
    items = []
    for row in _read(ITEM_INFO_CSV):
        if row["item_category"] not in ("consumable", "weapon", "armor", "accessory"):
            continue
        given = inputs.pop(row["name"])
        depth = given["earliest_pickup_depth"]
        items.append(Item(row["name"], row["item_category"], given["item_class"],
                          float(given["utility"]), float(depth) if depth else None,
                          desirability.get(row["name"])))
    if inputs:
        raise ValueError(f"worth_inputs.csv names items that item_info.csv lacks: {sorted(inputs)}")
    return items


def gold_names() -> List[str]:
    """Return the name of every gold amount a pickup holds, smallest first."""
    amounts = {int(row["simple_name"]) for row in _read(PICKUPS_CSV)
               if int(row["subtype_num"]) == MONEY_SUBTYPE}
    return [f"{amount} Gold" for amount in sorted(amounts)]


def ladder(items: List[Item], item_class: str) -> Tuple[Rung, ...]:
    """Return the rungs of ``item_class``'s ladder, or of all weapons for "weapon"."""
    members = [item for item in items
               if item.earliest_pickup_depth is not None and item.utility >= MIN_RUNG_UTILITY
               and (item.item_class in WEAPON_CLASSES if item_class == "weapon"
                    else item.item_class == item_class)]
    rungs: List[Rung] = []
    for item in sorted(members, key=lambda item: (item.earliest_pickup_depth, -item.utility)):
        if not rungs or item.utility > rungs[-1][0]:
            rungs.append((item.utility, item.earliest_pickup_depth, item.name))
    return tuple(rungs)


def _scale(item_class: str, utility: float) -> float:
    return math.log(max(utility, 1.0)) if item_class in LOG_SCALE_CLASSES else utility


def _depth_on(rungs: Tuple[Rung, ...], item_class: str, utility: float) -> Optional[float]:
    """Return the depth where ``rungs`` reach ``utility``, or None above the top rung."""
    if not rungs:
        return None
    first_utility, first_depth, _ = rungs[0]
    if utility <= first_utility:
        first = _scale(item_class, first_utility)
        return first_depth * (_scale(item_class, utility) / first) if first > 0 else first_depth
    for (low, low_depth, _), (high, high_depth, _) in zip(rungs, rungs[1:]):
        if utility <= high:
            share = ((_scale(item_class, utility) - _scale(item_class, low))
                     / (_scale(item_class, high) - _scale(item_class, low)))
            return low_depth + share * (high_depth - low_depth)
    return None


def class_fit_depth(item: Item, ladders: Dict[str, Tuple[Rung, ...]]) -> Optional[float]:
    """Return the depth where the castle's pickups of ``item``'s class offer as much as it does.

    Returns None for an item with no ladder, such as an accessory or a cure.
    """
    if item.item_class not in LADDER_CLASSES:
        return None
    own = ladders[item.item_class]
    if item.item_class in WEAPON_CLASSES:
        if own and own[0][0] <= item.utility <= own[-1][0]:
            return _depth_on(own, item.item_class, item.utility)
        across = _depth_on(ladders["weapon"], "weapon", item.utility)
        across = 1.0 if across is None else across
        return max(own[-1][1], across) if own and item.utility > own[-1][0] else across
    depth = _depth_on(own, item.item_class, item.utility)
    return 1.0 if depth is None else depth


def worth_row(item: Item, ladders: Dict[str, Tuple[Rung, ...]]) -> dict:
    """Return ``item``'s worth.csv row, with full-precision values."""
    fit = class_fit_depth(item, ladders)
    if item.name in FIXED_WORTH:
        worth, basis = FIXED_WORTH[item.name], "fixed"
    elif fit is not None:
        worth, basis = fit, "class ladder"
        if (item.category in ("weapon", "armor") and item.desirability is not None
                and item.desirability > fit):
            worth, basis = item.desirability, "desirability"
    elif item.desirability is not None:
        worth, basis = item.desirability, "desirability"
    else:
        worth = UNRATED_ACCESSORY_WORTH if item.category == "accessory" else UNRATED_OTHER_WORTH
        basis = "default"
    return dict(name=item.name, category=item.category, item_class=item.item_class,
                class_fit_depth=fit, desirability=item.desirability, worth=worth, basis=basis)


def _check_rounding(rows: List[dict]) -> None:
    """Raise if rounding to DECIMALS changes how any worth compares with the design's lines."""
    exact = [row["worth"] for row in rows]
    rounded = [round(value, DECIMALS) for value in exact]
    for line in LINES:
        if [(v > line) - (v < line) for v in exact] != [(v > line) - (v < line) for v in rounded]:
            raise ValueError(f"rounding to {DECIMALS} decimals moves an item across {line}")
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            near_exact = abs(exact[i] - exact[j]) <= WINDOW + 1e-9
            near_rounded = abs(rounded[i] - rounded[j]) <= WINDOW + 1e-9
            if near_exact != near_rounded:
                raise ValueError(f"rounding to {DECIMALS} decimals changes whether "
                                 f"{rows[i]['name']} and {rows[j]['name']} are within {WINDOW}")


def build() -> List[dict]:
    """Return every worth.csv row: the items in item number order, then gold."""
    items = load_items()
    ladders = {item_class: ladder(items, item_class)
               for item_class in LADDER_CLASSES + ("weapon",)}
    rows = [worth_row(item, ladders) for item in items]
    rows += [dict(name=name, category="money", item_class="gold", class_fit_depth=None,
                  desirability=None, worth=GOLD_WORTH, basis="fixed") for name in gold_names()]
    _check_rounding(rows)
    return rows


def _cell(value: Optional[float]) -> str:
    return "" if value is None else repr(round(value, DECIMALS))


def write_csv(rows: List[dict]) -> None:
    with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["name", "category", "item_class", "class_fit_depth", "desirability",
                         "worth", "basis"])
        for row in rows:
            writer.writerow([row["name"], row["category"], row["item_class"],
                             _cell(row["class_fit_depth"]), _cell(row["desirability"]),
                             _cell(row["worth"]), row["basis"]])


def main() -> None:
    rows = build()
    write_csv(rows)
    print(f"wrote {len(rows)} items -> {OUT_CSV.relative_to(HERE.parent.parent)}")


if __name__ == "__main__":
    main()
