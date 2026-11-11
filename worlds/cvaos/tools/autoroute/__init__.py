"""
Automated routing: working out by emulator search what a player needs to get around a room.

The plan is ``local_docs/AUTOMATED_ROUTING_HANDOVER.md``. The modules here are the parts that a
search is measured with and runs on, so they are written before any search.

Two of them are the benchmark, which needs no emulator:

* :mod:`.benchmark` turns the requirement sheets into labelled questions, and fixes which rooms
  are held out.
* :mod:`.scoring` compares a search's answers with those labels.

The others are the game as a search drives it:

* :mod:`.emulator` is the interface that a search needs from an emulator, with a fake one for
  unit tests.
* :mod:`.bizhawk` implements that interface on EmuHawk, which is the reference.
* :mod:`.game` is a session of the test ROM. It boots the game, sets loadouts and flags, and
  brings Soma into a room by a door.
* :mod:`.success` tells when Soma has left by a door, picked up an item, or reached or defeated
  an enemy.
* :mod:`.entries` gives the state that a search starts from at each door.
* :mod:`.traces` records the operations run on one emulator, so that another can be checked
  against them.
* :mod:`.reference` holds the short pieces of play whose traces EmuHawk recorded for that check.

``python -m worlds.cvaos.tools.autoroute`` prints the baselines, scores a file of answers,
writes the questions out as JSON, shows or measures the entry states, and records or replays
the reference traces.

This package is a development tool. Nothing in the world imports it.
"""
