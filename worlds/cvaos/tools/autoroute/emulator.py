"""
The emulator interface that the automated routing search drives.

:class:`Emulator` lists everything the search needs from an emulator: reading and writing memory,
running frames with chosen buttons held down, and saving and loading states. The game session in
``game.py`` and the success tests in ``success.py`` use nothing else, so they run unchanged on
every emulator that implements it.

There are two implementations here. ``bizhawk.py`` drives EmuHawk, which is the reference: another
emulator is right when it gives the same memory as EmuHawk for the same operations (see
``traces.py``). :class:`FakeEmulator` holds memory in a dictionary, for unit tests.

Addresses are full GBA bus addresses, such as 0x02000088.
"""
from __future__ import annotations

from typing import Callable, Hashable, Iterable, Optional, Protocol

BUTTONS = ("A", "B", "L", "R", "Start", "Select", "Up", "Down", "Left", "Right")


class Emulator(Protocol):
    """A console that runs the game only when asked to, one frame at a time.

    Game time passes only inside :meth:`step`. Every other method leaves the game on the frame it
    was on, so a write stays in memory until the next step, and two reads with no step between
    them return the same bytes.
    """

    def read(self, address: int, length: int) -> bytes:
        """Return ``length`` bytes of memory, starting at ``address``."""

    def write(self, address: int, data: bytes) -> None:
        """Write ``data`` to memory, starting at ``address``."""

    def step(self, buttons: Iterable[str] = (), frames: int = 1) -> None:
        """Run exactly ``frames`` frames with exactly ``buttons`` held down.

        ``buttons`` are names from :data:`BUTTONS`, and every other button is up for those frames.
        """

    def save(self) -> Hashable:
        """Save the console's whole state, and return a handle for it."""

    def load(self, state: Hashable) -> None:
        """Return the console to a saved state. The state can be loaded again afterwards."""

    def drop(self, state: Hashable) -> None:
        """Free a saved state, after which its handle can't be loaded."""

    def reset(self) -> None:
        """Switch the console off and on again, with a blank save in the cartridge.

        A blank save is one whose every byte is 0xFF. Every saved state is freed as well.
        """


def check_buttons(buttons: Iterable[str]) -> tuple[str, ...]:
    """Return ``buttons`` in the order of :data:`BUTTONS`. Raise ValueError for an unknown name."""
    chosen = set(buttons)
    unknown = sorted(chosen.difference(BUTTONS))
    if unknown:
        raise ValueError(f"unknown button {unknown[0]!r}; the buttons are {', '.join(BUTTONS)}")
    return tuple(name for name in BUTTONS if name in chosen)


def read_many(emulator: Emulator, ranges: Iterable[tuple[int, int]]) -> list[bytes]:
    """Return the bytes of each ``(address, length)`` range, in order.

    An emulator that has its own ``read_many`` method is asked for all the ranges at once, which
    saves a round trip per range on EmuHawk. The result is the same either way.
    """
    ranges = list(ranges)
    batch = getattr(emulator, "read_many", None)
    if batch is not None:
        return batch(ranges)
    return [emulator.read(address, length) for address, length in ranges]


def write_many(emulator: Emulator, writes: Iterable[tuple[int, bytes]]) -> None:
    """Write each ``(address, bytes)`` pair, in order.

    An emulator that has its own ``write_many`` method is given all the pairs at once.
    """
    writes = list(writes)
    batch = getattr(emulator, "write_many", None)
    if batch is not None:
        batch(writes)
        return
    for address, data in writes:
        emulator.write(address, data)


class FakeEmulator:
    """An :class:`Emulator` with no game in it, for unit tests.

    Memory is a dictionary from address to byte, and an address that was never written reads as
    0. ``frame`` counts the frames stepped since the last reset.

    A test supplies the game's behavior as two optional functions. ``on_frame(fake, buttons)`` is
    called once for each stepped frame, after ``frame`` has gone up, with the buttons held on that
    frame. ``on_reset(fake)`` is called at the end of each reset, and once when the fake is made.
    Both should keep everything they need in ``memory``, because a saved state holds ``memory``
    and ``frame`` and nothing else.
    """

    def __init__(self, on_frame: Optional[Callable[["FakeEmulator", tuple[str, ...]], None]] = None,
                 on_reset: Optional[Callable[["FakeEmulator"], None]] = None) -> None:
        self.memory: dict[int, int] = {}
        self.frame = 0
        self.on_frame = on_frame
        self.on_reset = on_reset
        self.states: dict[int, tuple[dict[int, int], int]] = {}
        self._next_state = 0
        if on_reset:
            on_reset(self)

    def read(self, address: int, length: int) -> bytes:
        return bytes(self.memory.get(address + offset, 0) for offset in range(length))

    def write(self, address: int, data: bytes) -> None:
        for offset, value in enumerate(data):
            self.memory[address + offset] = value

    def step(self, buttons: Iterable[str] = (), frames: int = 1) -> None:
        held = check_buttons(buttons)
        if frames < 0:
            raise ValueError("frames must be 0 or more")
        for _ in range(frames):
            self.frame += 1
            if self.on_frame:
                self.on_frame(self, held)

    def save(self) -> int:
        self._next_state += 1
        self.states[self._next_state] = (dict(self.memory), self.frame)
        return self._next_state

    def load(self, state: Hashable) -> None:
        memory, self.frame = self.states[state]
        self.memory = dict(memory)

    def drop(self, state: Hashable) -> None:
        del self.states[state]

    def reset(self) -> None:
        self.memory = {}
        self.states = {}
        self.frame = 0
        if self.on_reset:
            self.on_reset(self)
