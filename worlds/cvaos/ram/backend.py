"""
Transport backends for ``AoSRAM``.

``AoSRAM`` used to talk to BizHawk directly. The Steam Castlevania Advance Collection runs the
game in an emulator with no connector, so a client for it has to reach game memory another way
(attaching to the game process). The transport primitives therefore live behind a small backend
interface, and ``AoSRAM`` runs on whichever backend the client hands it:

* ``read_many([(offset, size, domain)]) -> [bytes]``: batched read. A batch should be a
  consistent snapshot when the transport can provide one (BizHawk executes the whole batch
  between two emulated frames; a process-memory transport cannot).
* ``write(offset, data, domain)``: plain write.
* ``guarded_write(offset, data, expected, domain) -> bool``: compare-and-write. The bytes are
  written only if they still equal ``expected``; False means the caller retries next tick.
  BizHawk's is frame-atomic; a read-compare-write implementation has a small race window that
  the retry contract already tolerates.

Domains are the BizHawk names ``"EWRAM"`` (offset relative to GBA 0x02000000) and ``"ROM"``
(offset = file offset, GBA 0x08000000-relative), the only two this world uses.

Error contract: each backend raises its own transport exception on connection loss
(``worlds._bizhawk.RequestFailedError`` for BizHawk); each client catches the one its transport
raises and retries or re-attaches.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Sequence

import worlds._bizhawk as bizhawk

if TYPE_CHECKING:
    from worlds._bizhawk import BizHawkContext


class RamBackend(Protocol):
    """The transport interface ``AoSRAM`` runs on. See the module docstring for semantics."""

    async def read_many(self, requests: Sequence[tuple[int, int, str]]) -> list[bytes]: ...

    async def write(self, offset: int, data: Sequence[int], domain: str) -> None: ...

    async def guarded_write(self, offset: int, data: Sequence[int],
                            expected: Sequence[int], domain: str) -> bool: ...


class BizHawkBackend:
    """The original transport: the BizHawk lua connector, one socket round-trip per call,
    with the connector's batch/frame atomicity guarantees."""

    def __init__(self, bizhawk_ctx: "BizHawkContext") -> None:
        self.ctx = bizhawk_ctx

    async def read_many(self, requests: Sequence[tuple[int, int, str]]) -> list[bytes]:
        return await bizhawk.read(self.ctx, list(requests))

    async def write(self, offset: int, data: Sequence[int], domain: str) -> None:
        await bizhawk.write(self.ctx, [(offset, list(data), domain)])

    async def guarded_write(self, offset: int, data: Sequence[int],
                            expected: Sequence[int], domain: str) -> bool:
        return await bizhawk.guarded_write(
            self.ctx, [(offset, list(data), domain)], [(offset, list(expected), domain)])
