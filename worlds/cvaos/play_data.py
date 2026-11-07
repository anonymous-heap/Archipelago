"""
A local log of each run's play data, so that the soul pace can be checked against real games.

The client appends one JSON object per line to one file per seed and slot. The file is named
like the seed's other files, ``AP_<seed>_P<slot>.jsonl``, and sits in the ``cvaos_play_data``
folder inside Archipelago's logs folder. A line is written only when something it records has
changed. Nothing here is sent to the server or to data storage.

Every line has ``event``, ``time`` and ``play_frames``. ``time`` is Unix time in whole seconds.
``play_frames`` is the save's play timer, which the game advances once per frame of play, 60 to a
second. The events are:

- ``session``: the client connected or attached to the game. The line names the ``seed``, the
  ``slot`` and the ``player``, and copies the slot data's numbers and strings as ``options``. The
  ``souls``, ``gear`` and ``drops_seen`` lines after it list everything again, not only what
  changed.
- ``souls``: the souls owned changed. ``owned`` counts the distinct souls of every kind, ability
  souls included, and ``gained`` and ``lost`` name the souls added and removed since the previous
  ``souls`` line.
- ``gear``: the equipped ``weapon`` or ``armor`` changed. Either is null when nothing is equipped.
- ``drops_seen``: the Enemy List shows new drops. ``slot_1`` names the enemies whose common drop
  it now shows, and ``slot_2`` the enemies whose rare drop it now shows.
- ``checks``: ``locations`` checked for the first time, as the client sends them.
- ``time``: a minute of play passed with no other line, or the play timer went back because an
  older save was loaded.

The souls come from the arrays of owned souls, not from the souls-collected count at 0x02013264,
because that count goes up on every absorption, repeats included.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Iterable

import Utils

from .data import enemy_type_collection, item_info_collection
from .locations import location_id_to_name
from .ram.accessors import PlayState

# A ``time`` line follows a minute of play without any other line.
TIME_LINE_FRAMES = 60 * 60

_ITEM_NAMES = {(info.item_category, info.id): info.name for info in item_info_collection}
_ENEMY_NAMES = {enemy.id: enemy.name for enemy in enemy_type_collection}


def folder() -> str:
    """Return the folder that holds the play-data files."""
    return Utils.user_path("logs", "cvaos_play_data")


def log_path(seed_name: str, slot: int) -> str:
    """Return the play-data file for one seed and slot."""
    return os.path.join(folder(), f"AP_{re.sub(r'[^A-Za-z0-9_-]', '_', seed_name)}_P{slot}.jsonl")


def _soul_names(souls: Iterable[tuple[str, int]]) -> list[str]:
    return sorted(_ITEM_NAMES.get(soul, f"{soul[0]} {soul[1]}") for soul in souls)


class PlayDataLog:
    """
    One session's writer for a seed and slot's play-data file.

    It keeps what its lines have said so far, so each new line records only a change. Each
    session gets a new writer, which starts with a ``session`` line and so states everything
    again.
    """

    def __init__(self, path: str, session: dict) -> None:
        self.path = path
        self.session: dict | None = session  # the session line's fields, until it is written
        self.souls: frozenset[tuple[str, int]] | None = None
        self.gear: tuple[int, int] | None = None
        self.drops_seen: tuple[frozenset[int], frozenset[int]] = (frozenset(), frozenset())
        self.last_line_frames = 0
        self.failed = False

    def record(self, state: PlayState) -> None:
        """Write a line for each recorded value that differs from what the file last said."""
        frames = state.play_frames
        if self.session is not None:
            self._write("session", frames, **self.session)
            self.session = None
        if state.souls != self.souls:
            known = self.souls or frozenset()
            self._write("souls", frames, owned=len(state.souls),
                        gained=_soul_names(state.souls - known),
                        lost=_soul_names(known - state.souls))
            self.souls = state.souls
        if (state.weapon, state.armor) != self.gear:
            self._write("gear", frames, weapon=_ITEM_NAMES.get(("weapon", state.weapon)),
                        armor=_ITEM_NAMES.get(("armor", state.armor)))
            self.gear = (state.weapon, state.armor)
        new_drops = [sorted(_ENEMY_NAMES.get(enemy, str(enemy)) for enemy in seen - known)
                     for seen, known in zip(state.drops_seen, self.drops_seen)]
        if any(new_drops):
            self._write("drops_seen", frames, slot_1=new_drops[0], slot_2=new_drops[1])
        self.drops_seen = state.drops_seen
        if not self.last_line_frames <= frames < self.last_line_frames + TIME_LINE_FRAMES:
            self._write("time", frames)

    def checks(self, play_frames: int, location_ids: Iterable[int]) -> None:
        """Write the location checks the client has just sent for the first time."""
        self._write("checks", play_frames, locations=sorted(
            location_id_to_name.get(location, str(location)) for location in location_ids))

    def _write(self, event: str, play_frames: int, **fields) -> None:
        self.last_line_frames = play_frames
        if self.failed:
            return
        line = json.dumps({"event": event, "time": int(time.time()), "play_frames": play_frames,
                           **fields})
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as file:
                file.write(line + "\n")
        except OSError as error:
            # The log must never stop the client, so a failure turns it off for this session.
            from CommonClient import logger
            self.failed = True
            logger.warning("CVAoS: could not write the play-data log %s (%s).", self.path, error)
