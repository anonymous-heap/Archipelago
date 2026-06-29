"""
Custom-pickup CONTENT registry for Castlevania: Aria of Sorrow.

This is the file you edit to ADD A PICKUP -- it is pure data. The machinery (the collect hook, the
Item-Use menu, the icon pipeline, the byte builders) lives in custom_pickups.py and rom/icon.py and
should not need touching to add a pickup.

To add one:
  1. Choose an icon SOURCE (see rom/icon.py):
       * ImageFile("my_icon.png") -- your own ORIGINAL 16x16 art under rom/icons/ (quantised to bank 6).
       * RomSprite(gfx_addr=..., part=(x, y, w, h), remap={src: bank6, ...}) -- the pixel pattern is
         extracted from the PLAYER's own ROM at build time (for art already in the game; ships no
         copyrighted pixels). Resolving a new sprite's gfx/part/palette is per-sprite RE.
  2. Add a CustomPickup(...) row below and append it to CUSTOM_PICKUPS. Pick a unique item_offset
     (32..0x2B) and icon_id (free range 0x1f..0x40); collisions fail loudly at import.
  3. To spawn it: dev-test via CUSTOM_PICKUP_TEST_PLACEMENTS (force a location to spawn it), or wire
     real randomizer placement (not yet integrated).
"""
from __future__ import annotations

from typing import Dict, List

from .custom_pickups import (
    CustomPickup,
    validate_registry,
    FLAG_FIELD_EVENT,    # 0x33C  SetEventFlag field
    FLAG_FIELD_MISC,     # 0x344  the A01 press-button's field
    FLAG_FIELD_PICKUP,   # 0x360  per-location "collected" field
    FLAG_FIELD_BOSS,     # 0x37E  boss-death field
)
from .icon import RomSprite, ImageFile

# --- Study Sealswitch (the first custom pickup) -----------------------------------------------------
# Icon: the in-game metal-gate-button (special object 0x35). Its sprite GFX is LZ77-compressed at
# 0x08604428 (a 1D page, 16 tiles/row); frame 2's button content is the 16x16 region at page pixel
# (32, 5). rom/icon.py extracts that pattern from the PLAYER's ROM at build time, so no copyrighted
# pixels ship -- only this address, the part rectangle, and the source-index -> bank-6 remap below
# (gold ramp src 2-7 -> tan #F8B070(14)/gold #F8D848(7); white 8 -> #F8F8F8(15); blue-grey 10-14 ->
# #605888(3)/#A0A0A8(4)/#E8D8D0(5)). The remap is a palette-index mapping, not copyrightable.
_BUTTON_REMAP = {2: 14, 3: 14, 4: 14, 5: 7, 6: 7, 7: 7, 8: 15, 10: 3, 11: 4, 12: 4, 13: 4, 14: 5}
_BUTTON_ICON = RomSprite(gfx_addr=0x08604428, part=(32, 5, 16, 11), remap=_BUTTON_REMAP, crop=True)

STUDY_SEALSWITCH = CustomPickup(
    name="Study Sealswitch",
    item_offset=32,                # first custom / Item-Use slot (32..0x2B)
    icon_id=0x1F,                  # first free icon id (sheet 0) -- the floor pickup sprite
    icon=_BUTTON_ICON,             # extracted from the player's ROM at build (gold/silver via bank 6)
    flag_field=FLAG_FIELD_MISC,    # the A01 button writes the MISC field
    flag_number=48,                # misc flag #48 (0x02000348 bit 16) -> barrier sinks
    sfx=0x133,                     # the button's SFX/song
    inventory_name="Study Sealswitch",
    description=("The Study's underground egress was",
                 "supposed to be forever sealed..."),
)

# --- The registry (append new pickups here) ---------------------------------------------------------
CUSTOM_PICKUPS: List[CustomPickup] = [STUDY_SEALSWITCH]

# Backwards-compat alias (older references).
FORBIDDEN_AREA_BUTTON = STUDY_SEALSWITCH

# --- Test placements (dev): map a location's display_name -> a CustomPickup to force it to spawn
# that custom pickup instead of its rolled item. Empty by default (the framework is inert without a
# placement). To try the button in-game, e.g.:  {"Ancient Book 1": STUDY_SEALSWITCH}
CUSTOM_PICKUP_TEST_PLACEMENTS: Dict[str, CustomPickup] = {}

validate_registry(CUSTOM_PICKUPS)   # fail loudly on duplicate item_offset / icon_id
