"""
The AoS cartridge as a bytemaker address space.

Every ROM feature module used to restate the same two facts — the cart is mapped at
``0x08000000`` and is little-endian — inside its own ``GBA - base`` arithmetic. A ``Space``
states them once; ``Entry`` declarations then address the ROM by GBA address and the
``file_offset = gba - base`` conversion happens in one place.

The Steam Castlevania Advance Collection ships its own copy of the ROM, and M2 changed it in 23
places. Most are small patches to the game's code and data, and one zeroes a sound-mixer routine.
Two are additions in what is free space on a cart. Those two are zero on a cart dump and live on
the collection image, so an AP write there that is harmless on a cart corrupts the collection.
They are declared here as the ``M2_NO_GO`` regions, so placements can be checked against them
rather than against a comment.

The other changes sit inside the game's own code and data. A hook that replays the instructions
it overwrites must not be placed on any of them, because the collection's instructions there
differ from the cart's.

M2's code also writes to high EWRAM that the game itself never uses. Those addresses are
declared as ``M2_EWRAM``, and AP's scratch memory in that area must avoid them as well.
"""
from __future__ import annotations

from typing import Optional

from .._bytemaker_compat import Buffer, Entry, Space
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


# --- Advance Collection ROM: M2's additions (file 0x660000-0x6610BC and 0x700000-0x7000E3) ---
M2_GRAPHIC = Entry(0x08660000, Buffer.of(nbytes=0x10BD), name="M2 replacement graphic",
                   note="Advance Collection ROM only; LZ77 graphic repointed via 0x1CC018")
M2_AUDIO_BRIDGE = Entry(0x08700000, Buffer.of(nbytes=0xE4), name="M2 audio bridge",
                        note="Advance Collection ROM only")
M2_NO_GO = (M2_GRAPHIC, M2_AUDIO_BRIDGE)

# --- Advance Collection: EWRAM that M2's code writes ---
# The patch at 0x0800EA9A calls M2's code at 0x08700000. That code appends 2-byte entries to a
# 256-entry ring at 0x0202FC00 and keeps the ring's index byte at 0x0202FF40. Both are in the
# high-EWRAM tail (0x02025554..0x02040000) that the game never touches, so they are free on a cart
# and in use on the collection.
M2_EWRAM = (range(0x0202FC00, 0x0202FE00), range(0x0202FF40, 0x0202FF41))


def m2_region_hit(file_offset: int, nbytes: int) -> Optional[Entry]:
    """The M2 region that a write of ``nbytes`` at ``file_offset`` would touch, or None."""
    geometry = gba_space()
    for region in M2_NO_GO:
        start, size = region.bind(geometry).request()
        if file_offset < start + size and start < file_offset + nbytes:
            return region
    return None
