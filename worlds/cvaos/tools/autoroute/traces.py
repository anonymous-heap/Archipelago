"""
Traces: a record of the operations run on one emulator, to replay on another.

A trace lists every :class:`~.emulator.Emulator` operation of a run in order, and it holds the
bytes that each read returned. :func:`replay` runs the same operations on another emulator and
compares what its reads return. An emulator is a faithful stand-in for the one that made the
trace when every read matches.

There are three ways to use a trace:

* :class:`Recording` wraps an emulator and builds a trace of everything asked of it.
* :func:`replay` runs a trace on an emulator and returns each read that differs.
* :class:`Playback` is an emulator that answers from a trace alone, so code that was recorded
  can run again with no emulator at all.

A trace file is JSON lines. The first line is an object that describes the run, and each later
line is one operation::

    ["reset"]
    ["step", ["A", "Left"], 3]            the buttons held, then the number of frames
    ["write", "0x020003F8", "0c0b..."]    an address, then the bytes in hexadecimal
    ["read", "0x02000526", "7001"]        an address, then the bytes that the read returned
    ["digest", "0x02000000", 262144, "9e10..."]   an address, a length, and the md5 of that memory
    ["save", 0]                           states are numbered from 0 in the order they are saved
    ["load", 0]
    ["drop", 0]
    ["note", "any text"]                  a label for a person, which replaying ignores

A digest stands for a read of memory that is too large to keep, such as all of the work RAM.
"""
from __future__ import annotations

import hashlib
import json
from typing import Hashable, Iterable, NamedTuple, Optional, Sequence

from .emulator import Emulator, check_buttons, read_many, write_many

FORMAT = "cvaos-autoroute-trace-1"
# The two blocks of work RAM, as (address, length). EWRAM holds all the game state that the
# search reads. IWRAM also holds the BIOS's own variables and the stack, so two emulators that
# agree on EWRAM can differ there when they start the BIOS differently.
EWRAM = (0x02000000, 0x40000)
IWRAM = (0x03000000, 0x8000)


class TraceError(Exception):
    """A trace file is malformed, or a :class:`Playback` was asked for something else than
    its trace holds next."""


def _address(text: str) -> int:
    return int(text, 16)


class Recording:
    """An emulator that passes every operation to another one and keeps a trace of them.

    ``ops`` is the trace so far. A state's handle is its number in the trace, which counts up
    from 0 over the whole recording and doesn't start again at a reset.
    """

    def __init__(self, emulator: Emulator) -> None:
        self.emulator = emulator
        self.ops: list[list] = []
        self._states: dict[int, Hashable] = {}
        self._next_state = 0

    def read_many(self, ranges: Iterable[tuple[int, int]]) -> list[bytes]:
        ranges = list(ranges)
        blocks = read_many(self.emulator, ranges)
        for (address, _), data in zip(ranges, blocks):
            self.ops.append(["read", f"{address:#010x}", bytes(data).hex()])
        return blocks

    def read(self, address: int, length: int) -> bytes:
        return self.read_many([(address, length)])[0]

    def write_many(self, writes: Iterable[tuple[int, bytes]]) -> None:
        writes = list(writes)
        write_many(self.emulator, writes)
        for address, data in writes:
            self.ops.append(["write", f"{address:#010x}", bytes(data).hex()])

    def write(self, address: int, data: bytes) -> None:
        self.write_many([(address, data)])

    def step(self, buttons: Iterable[str] = (), frames: int = 1) -> None:
        held = check_buttons(buttons)
        self.emulator.step(held, frames)
        self.ops.append(["step", list(held), frames])

    def save(self) -> int:
        number = self._next_state
        self._states[number] = self.emulator.save()
        self._next_state += 1
        self.ops.append(["save", number])
        return number

    def load(self, state: Hashable) -> None:
        self.emulator.load(self._states[state])
        self.ops.append(["load", state])

    def drop(self, state: Hashable) -> None:
        self.emulator.drop(self._states.pop(state))
        self.ops.append(["drop", state])

    def reset(self) -> None:
        self.emulator.reset()
        self._states.clear()
        self.ops.append(["reset"])

    def digest(self, address: int, length: int) -> str:
        """Record the md5 of ``length`` bytes of memory at ``address``, and return it."""
        found = hashlib.md5(self.emulator.read(address, length)).hexdigest()
        self.ops.append(["digest", f"{address:#010x}", length, found])
        return found

    def note(self, text: str) -> None:
        """Record a label, which says what the operations after it are for."""
        self.ops.append(["note", text])


def save_trace(path: str, about: dict, ops: Sequence[list]) -> None:
    """Write a trace file.

    ``about`` describes the run. It becomes the file's first line, together with the format's
    name under ``"format"``.
    """
    first = {"format": FORMAT, **{key: value for key, value in about.items() if key != "format"}}
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(first) + "\n")
        for op in ops:
            handle.write(json.dumps(op, separators=(",", ":")) + "\n")


def load_trace(path: str) -> tuple[dict, list[list]]:
    """Read a trace file, and return its description and its operations.

    Raise :class:`TraceError` for a file that isn't a trace in this format.
    """
    with open(path, encoding="utf-8") as handle:
        lines = [line for line in handle if line.strip()]
    try:
        about = json.loads(lines[0]) if lines else {}
        if not isinstance(about, dict) or about.get("format") != FORMAT:
            raise TraceError(f"{path} isn't a trace file in the {FORMAT} format.")
        ops = [json.loads(line) for line in lines[1:]]
    except json.JSONDecodeError as error:
        raise TraceError(f"{path} isn't a trace file: {error}") from error
    for number, op in enumerate(ops, 2):
        if not isinstance(op, list) or not op:
            raise TraceError(f"Line {number} of {path} isn't an operation: {op!r}")
    return about, ops


class Mismatch(NamedTuple):
    """A read or a digest that gave something else than its trace holds."""

    index: int    # the operation's position in the trace, from 0
    # The frames that the game has run since the last reset. Loading a state returns the count to
    # what it was when the state was saved.
    frames: int
    op: list      # the operation, which holds the bytes or the md5 that the trace expects
    got: str      # the bytes in hexadecimal, or the md5, that the emulator gave

    def __str__(self) -> str:
        return (f"operation {self.index}, {self.frames} frames after the reset: {self.op[0]} at"
                f" {self.op[1]} gave {self.got}, and the trace has {self.op[-1]}")


def replay(ops: Sequence[list], emulator: Emulator, digests: bool = True,
           stop_after: Optional[int] = None) -> list[Mismatch]:
    """Run a trace's operations on ``emulator``, and return the reads that differ from it.

    Every operation runs whether or not an earlier read differed, unless ``stop_after`` is given,
    in which case the replay ends once it has found that many mismatches. Digests are compared
    like reads, or skipped when ``digests`` is false.

    An emulator can put the boundary between two frames at another point of the frame than the
    one that made the trace does. Its reads are then those of the trace one frame early or late,
    and the mismatches start at the first read after a step.
    """
    found: list[Mismatch] = []
    states: dict[int, Hashable] = {}
    saved_at: dict[int, int] = {}   # the frame count at which each state was saved
    frames = 0
    for index, op in enumerate(ops):
        kind = op[0]
        if kind == "read":
            got = bytes(emulator.read(_address(op[1]), len(op[2]) // 2)).hex()
            if got != op[2]:
                found.append(Mismatch(index, frames, op, got))
        elif kind == "digest":
            if digests:
                got = hashlib.md5(emulator.read(_address(op[1]), op[2])).hexdigest()
                if got != op[3]:
                    found.append(Mismatch(index, frames, op, got))
        elif kind == "write":
            emulator.write(_address(op[1]), bytes.fromhex(op[2]))
        elif kind == "step":
            emulator.step(op[1], op[2])
            frames += op[2]
        elif kind == "save":
            states[op[1]] = emulator.save()
            saved_at[op[1]] = frames
        elif kind == "load":
            emulator.load(states[op[1]])
            frames = saved_at[op[1]]
        elif kind == "drop":
            emulator.drop(states.pop(op[1]))
            del saved_at[op[1]]
        elif kind == "reset":
            emulator.reset()
            states.clear()
            saved_at.clear()
            frames = 0
        elif kind != "note":
            raise TraceError(f"operation {index} is of an unknown kind: {op!r}")
        if stop_after is not None and len(found) >= stop_after:
            break
    return found


class Playback:
    """An emulator that answers from a trace, with no game running.

    Each call must be the trace's next operation, with the same arguments. A read then returns
    the bytes that the trace holds. Any other call raises :class:`TraceError`, because the trace
    can't say what the game would have done. The buttons of a step may be in any order, in the
    trace and in the call.

    Notes are passed over. Digests are too, except by :meth:`digest`, which returns the next
    one. ``done`` is true once the trace has nothing left but notes and digests.
    """

    def __init__(self, ops: Sequence[list]) -> None:
        self.ops = [list(op) for op in ops]
        for index, op in enumerate(self.ops):
            if op[0] == "step":
                try:
                    op[1] = list(check_buttons(op[1]))
                except ValueError as error:
                    raise TraceError(f"Operation {index} of the trace is {op!r}:"
                                     f" {error}") from error
        self.at = 0

    def _skip(self, kinds: tuple[str, ...]) -> None:
        while self.at < len(self.ops) and self.ops[self.at][0] in kinds:
            self.at += 1

    def _next(self, *asked) -> list:
        self._skip(("note",) if asked[0] == "digest" else ("note", "digest"))
        if self.at >= len(self.ops):
            raise TraceError(f"The trace has ended, and the call was {list(asked)!r}.")
        op = self.ops[self.at]
        if op[:len(asked)] != list(asked):
            raise TraceError(f"Operation {self.at} of the trace is {op!r}, and the call was"
                             f" {list(asked)!r}.")
        self.at += 1
        return op

    @property
    def done(self) -> bool:
        return all(op[0] in ("note", "digest") for op in self.ops[self.at:])

    def read(self, address: int, length: int) -> bytes:
        data = bytes.fromhex(self._next("read", f"{address:#010x}")[2])
        if len(data) != length:
            raise TraceError(f"Operation {self.at - 1} of the trace read {len(data)} bytes at"
                             f" {address:#010x}, and the call asked for {length}.")
        return data

    def write(self, address: int, data: bytes) -> None:
        self._next("write", f"{address:#010x}", bytes(data).hex())

    def step(self, buttons: Iterable[str] = (), frames: int = 1) -> None:
        self._next("step", list(check_buttons(buttons)), frames)

    def save(self) -> int:
        return self._next("save")[1]

    def load(self, state: Hashable) -> None:
        self._next("load", state)

    def drop(self, state: Hashable) -> None:
        self._next("drop", state)

    def reset(self) -> None:
        self._next("reset")

    def digest(self, address: int, length: int) -> str:
        """Return the md5 that the trace's next digest holds for this memory."""
        return self._next("digest", f"{address:#010x}", length)[3]
