"""
Live-memory (EWRAM) access for the Aria of Sorrow BizHawk client.

- ``addresses`` — the AoS EWRAM map: ``Entry`` declarations on the EWRAM address plane
  (``ewram``), plus ``GameState``, the bit masks, and the ``INVENTORY`` table. Pure
  declarations; importable without BizHawk.
- ``AoSRAM`` — async typed get/set helpers over those entries for one BizHawk connection.
- ``PlayerVitals`` / ``EquippedGear`` / ``SoulPair`` — bytemaker structs for the record
  blocks and the nibble-packed soul bytes the entries decode.

Every address here was verified against the USA ROM. ``addresses`` declares each one by its
full GBA address; ``entry.request()`` yields the EWRAM-domain offset BizHawk expects.
"""
from . import addresses
from .accessors import AoSRAM
from .addresses import INVENTORY, GameState, InventoryArray
from .structures import EquippedGear, PlayerVitals, SoulPair

__all__ = [
    "addresses",
    "AoSRAM",
    "INVENTORY",
    "InventoryArray",
    "GameState",
    "PlayerVitals",
    "EquippedGear",
    "SoulPair",
]
