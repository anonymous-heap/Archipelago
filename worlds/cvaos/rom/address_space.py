"""
The AoS cartridge as a bytemaker address space.

Every ROM feature module used to restate the same two facts — the cart is mapped at
``0x08000000`` and is little-endian — inside its own ``GBA - base`` arithmetic. A ``Space``
states them once; ``Entry`` declarations then address the ROM by GBA address and the
``file_offset = gba - base`` conversion happens in one place.
"""
from __future__ import annotations

from typing import Optional

from .._bytemaker_compat import Space
from .entity import GBA_ROM_BASE

ROM_SIZE = 0x800000  # exactly 8 MiB, cart dump and Advance Collection alike


def gba_space(buf: Optional[bytes] = None) -> Space:
    """The cart as a ``Space``: over an image when we have one, geometry-only when we do not.

    Generation never has the player's ROM, so a feature that builds writes blind (nothing to
    read, only addresses to place things at) asks for the geometry-only form and records its
    edits into a ``Patch``. A feature that must read the ROM first (to verify or transform
    bytes) binds over the image.
    """
    if buf is None:
        return Space(None, size=ROM_SIZE, base=GBA_ROM_BASE, endian="little", name="AoS")
    return Space(buf, base=GBA_ROM_BASE, endian="little", name="AoS")
