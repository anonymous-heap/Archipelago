"""
Test runs in EmuHawk, the attempt log, and what the runs say about a pair's requirement.

A *run* starts with Soma at a door with a chosen *loadout* (the movement souls switched on or
equipped). It ends when he defeats or reaches the target placement, or when the tester gives up.
If he leaves the room, the run starts over at the door, because a route through other rooms
belongs to those rooms' doors. The *used set* is every movement soul that was on at any moment
during the run, since a tester may swap souls in the menu. Each finished run is one line of the
attempt log, ``attempts.jsonl`` in the annotator's data folder.

Logic assumes that switching a soul on never makes a place unreachable. So for one door and
placement, the loadouts that succeed form an upward-closed family of sets, and its minimal
members are the ways through. A success with used set U settles every superset of U as a success,
and a failure with used set U settles every subset of U as a failure. :func:`summarize` reports the
minimal successes found so far, and the smallest loadouts not yet settled either way.
"""
from __future__ import annotations

import datetime
import itertools
import json
import os
from dataclasses import dataclass, field
from typing import Optional

# The souls a suggestion considers. Galamoth and Chronomage rarely matter for movement, so they
# are left out of suggestions, though a run can still use them.
CANDIDATES = ("Glide", "Slide", "DJump", "HJump", "WWalk", "Dive", "Panth", "Bat", "BDash",
              "Kick", "Tform")
REACH_DISTANCE = 40   # pixels between Soma's feet and the placement's spawn point


@dataclass
class Run:
    """One test run, while it is going and once it has ended."""
    enemy: int
    name: str
    room: str
    door: str                       # entrance node, such as "90D:917"
    target: tuple[int, int]         # the placement's spawn point, in room pixels
    type_id: int
    loadout: list[str]
    room_address: int = 0
    # The room entry the target spawns from, which its entity carries (see
    # emulator.room_entity_id). None when it can't be found in the ROM.
    entity_id: Optional[int] = None
    # True when the room holds another placement of the same type. The game's defeated bit is per
    # type, so then only the target's own entity can show that it was the one defeated.
    twins: bool = False
    used: set[str] = field(default_factory=set)
    outcome: Optional[str] = None   # "defeated", "reached" or "failed"
    distance: Optional[int] = None
    started: str = field(default_factory=lambda: _now())

    @property
    def defeat_counts(self) -> bool:
        """Whether a defeat of the target can be told apart from a defeat of its type."""
        return not self.twins or self.entity_id is not None

    def status(self) -> dict:
        return {"enemy": self.enemy, "name": self.name, "room": self.room, "door": self.door,
                "loadout": self.loadout, "used": sorted(self.used), "outcome": self.outcome,
                "distance": self.distance, "defeat_counts": self.defeat_counts}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def log_path(data_dir: str) -> str:
    return os.path.join(data_dir, "attempts.jsonl")


def append(data_dir: str, run: Run, annotator: str) -> None:
    """Add a finished run to the attempt log."""
    entry = {"time": run.started, "by": annotator, "room": run.room, "door": run.door,
             "enemy": run.enemy, "name": run.name, "loadout": sorted(run.loadout),
             "used": sorted(run.used), "outcome": run.outcome}
    with open(log_path(data_dir), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def load(data_dir: str) -> list[dict]:
    try:
        with open(log_path(data_dir), encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except FileNotFoundError:
        return []


def _minimal(sets: list[frozenset]) -> list[frozenset]:
    out = []
    for candidate in sorted(set(sets), key=lambda s: (len(s), sorted(s))):
        if not any(kept <= candidate for kept in out):
            out.append(candidate)
    return out


def summarize(entries: list[dict], limit: int = 6) -> dict[str, dict]:
    """Return, for each "door|enemy" with runs, what the runs settle.

    ``ways`` are the minimal successful used sets. ``next`` lists up to ``limit`` of the smallest
    loadouts that no run settles yet, built from :data:`CANDIDATES`. ``contradictions`` lists
    successes that a failure contains, which usually means a timing mistake or an enemy that
    moved, and calls for a retest.
    """
    by_pair: dict[str, dict[str, list[frozenset]]] = {}
    for entry in entries:
        key = f"{entry['door']}|{entry['enemy']}"
        kind = "failed" if entry["outcome"] == "failed" else "succeeded"
        by_pair.setdefault(key, {"succeeded": [], "failed": []})[kind].append(
            frozenset(entry["used"]))
    out = {}
    for key, runs in by_pair.items():
        ways = _minimal(runs["succeeded"])
        failures = runs["failed"]
        contradictions = [sorted(way) for way in ways if any(way <= failed for failed in failures)]
        out[key] = {"ways": [sorted(way) for way in ways], "failures": len(failures),
                    "successes": len(runs["succeeded"]),
                    "next": _unsettled(ways, failures, limit), "contradictions": contradictions}
    return out


def _unsettled(ways: list[frozenset], failures: list[frozenset], limit: int) -> list[list[str]]:
    """Return up to ``limit`` of the smallest loadouts of at most two souls that no run settles."""
    out = []
    for size in range(0, 3):
        for combo in itertools.combinations(CANDIDATES, size):
            loadout = frozenset(combo)
            if not (any(way <= loadout for way in ways)
                    or any(loadout <= failed for failed in failures)):
                out.append(sorted(loadout))
                if len(out) >= limit:
                    return out
    return out
