"""
Aria of Sorrow live-memory map (EWRAM) for the BizHawk client.

Every location is an ``Entry`` on the EWRAM address plane, declared by its full GBA address.
BizHawk's ``"EWRAM"`` memory domain wants offsets from the EWRAM base ``0x02000000``, and
``entry.request()`` returns exactly that ``(offset, nbytes)`` pair, so the subtraction lives in
the ``Space`` rather than beside every constant. Fields of the two record blocks
(``PlayerVitals``, ``EquippedGear``) are addressed through their layout, so the per-field
entries below cannot drift from the structs.

Values (state codes, bit masks, indices) stay plain ints. Every address was verified against
the USA ROM.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .._bytemaker_compat import Buffer, Entry, Space, count, u8, u16, u32, unknown
from .structures import EquippedGear, PlayerVitals, SoulPair

# --- Memory/BizHawk address-section names ---
EWRAM = "EWRAM"

EWRAM_BASE = 0x02000000
EWRAM_SIZE = 0x40000

#: The EWRAM address plane: geometry only, because the bytes arrive from the emulator.
ewram = Space(None, size=EWRAM_SIZE, base=EWRAM_BASE, endian="little", name="EWRAM")


# --- Game state / "safe to act" gating ---
GAME_STATE = ewram.entry(0x02000010, u8, name="game_state")
MENU_STATE = ewram.entry(0x02000064, u8, name="menu_state")

# 0x020000A1 u8 "current game mode": low nibble is the character (Soma=1 / Julius=0), high nibble
# is the difficulty (normal=0 / hard=1). The game writes it only at new-game / mode-select; the
# damage scaling, soul-drop rates, and the HARD_PICKUP spawn gate all read it live, so forcing the
# high nibble to 1 makes the game behave as Hard Mode.
GAME_MODE = ewram.entry(0x020000A1, u8, name="game_mode")
GAME_MODE_DIFFICULTY_MASK = 0xF0   # high nibble
GAME_MODE_HARD = 0x10              # high nibble == 1

# 0x02000060 u32 "cleared-data" flags. The game sets this to 3 (bits 0+1) when you beat it.
# Value 3 marks a cleared file: cutscenes become Start-skippable, and the new-game menu's Hard Mode
# prompt is offered (it checks bit 1). We force it for every randomized ROM so cutscenes are
# skippable. The bits live in the low byte, which is all the client touches.
GAME_CLEARED_FLAGS = ewram.entry(0x02000060, u8, name="game_cleared_flags")
GAME_CLEARED_VALUE = 0x03          # bits 0+1 set == game beaten once


# Game modes, named after the decomp's GAME_MODE_* enum (constants/main.h); 0x03 is unnamed there too.
class GameState(IntEnum):
    KONAMI_LOGO = 0x00
    TITLE = 0x01
    MAIN_MENU = 0x02
    INGAME = 0x04
    GAME_OVER = 0x05
    CREDITS = 0x06
    INTRO_CUTSCENE = 0x07


MENU_STATE_NORMAL = 0x01            # in-room, not transitioning/paused/shopping
MENU_STATE_DEATH = 0x02             # death/game-over fade sub-state (drives DeathLink detection)
MENU_STATE_ROOM_TRANSITION = 0x03
MENU_STATE_MAP_SCREEN = 0x04        # the Select map (observed live). Saving does not leave NORMAL: the save
                                    # prompt and the save write happen while the client may still grant items.
MENU_STATE_PAUSE = 0x06
MENU_STATE_WARP_MAP = 0x07          # warp-room map screen (observed live in the collection)
MENU_STATE_ITEM_WARP = 0x08         # warp in progress after using the Skull Key from the pause menu (observed live)
MENU_STATE_SHOP = 0x09


# --- Location detection: collected-pickup save flags ---
# 0x02000360, 20 bytes / 160 bits. A byte blob, so it decodes straight to ``bytes``.
PICKUP_FLAGS = ewram.entry(0x02000360, Buffer.of(nbytes=0x14), name="pickup_flags")
PICKUP_FLAGS_LEN = PICKUP_FLAGS.size


# --- Progress / goal flags ---
EVENT_FLAGS = ewram.entry(0x0200033C, u8, unknown("event flags; Phase 6 autotracking"), name="event_flags")
BOSS_FLAGS = ewram.entry(0x0200037E, u16, name="boss_flags")
GLOBAL_FLAGS = ewram.entry(0x0200042C, u32, name="global_flags")
CURRENT_AREA = ewram.entry(0x0200009E, u8, name="current_area")          # Phase 6 (area-aware DeathLink)
CURRENT_ROOM = ewram.entry(0x0200009F, u8, name="current_room")
CURRENT_SAVE_SLOT = ewram.entry(0x02000428, u8, name="current_save_slot")

BOSS_FLAG_GRAHAM = 0x0001          # bad-ending final boss
BOSS_FLAG_DEATH = 0x0002
BOSS_FLAG_CHAOS = 0x0080           # true-ending final boss, fallback if global doesn't work
GLOBAL_FLAG_GOOD_ENDING = 0x00004000


# --- Player inventory / stats block ---
GEAR = ewram.entry(0x02013268, EquippedGear, name="equipped_gear")
VITALS = ewram.entry(0x0201327A, PlayerVitals, name="vitals")

# One entry per field, addressed through the record layout rather than by a hand-kept offset.
EQUIPPED_WEAPON = GEAR.field("weapon")             # 0x02013268 u8
EQUIPPED_RED_SOUL = GEAR.field("red_soul")         # 0x02013269
EQUIPPED_BLUE_SOUL = GEAR.field("blue_soul")       # 0x0201326A
EQUIPPED_YELLOW_SOUL = GEAR.field("yellow_soul")   # 0x0201326B
EQUIPPED_ARMOR = GEAR.field("armor")               # 0x0201326C
EQUIPPED_ACCESSORY = GEAR.field("accessory")       # 0x0201326D
CURRENT_HP = VITALS.field("current_hp")            # 0x0201327A s16
CURRENT_MP = VITALS.field("current_mp")            # 0x0201327C s16
MAX_HP = VITALS.field("max_hp")                    # 0x0201327E u16
MAX_MP = VITALS.field("max_mp")                    # 0x02013280 u16
CURRENT_GOLD = ewram.entry(0x02013290, u32, name="current_gold")

# pad_1328A: verified-dead, saved, zeroed-on-new-game.
AP_RECEIVED_COUNT = ewram.entry(0x0201328A, u16, name="ap_received_count")

# DeathLink kill-request flag: the client writes 1; the ROM hook (rom/deathlink_hook.py) calls the
# game's real death routine and clears it. pad_1324C, verified dead live (mGBA) -- never written by
# the engine across combat / menus / shop / save / load.
KILL_REQUEST = ewram.entry(0x0201324C, u8, name="kill_request")


@dataclass(frozen=True)
class InventoryArray:
    """
    One of AoS's owned-item count arrays: an ``Entry`` over its bytes plus the item scheme.

    Byte arrays hold one count per byte. Soul arrays nibble-pack two souls per byte, one
    ``SoulPair`` record each (low nibble = even index, high nibble = odd). ``entry.item(i)``
    does the base + index arithmetic either way. Souls also have an unused secondary list,
    which we ignore.
    """

    name: str
    entry: Entry
    length: int          # number of items the array can address
    nibble_packed: bool


def _byte_array(name: str, gba_addr: int, items: int) -> InventoryArray:
    return InventoryArray(name, ewram.entry(gba_addr, u8, count(items), name=name), items, False)


def _soul_array(name: str, gba_addr: int, items: int) -> InventoryArray:
    pairs = (items + 1) // 2
    return InventoryArray(name, ewram.entry(gba_addr, SoulPair, count(pairs), name=name), items, True)


# Keyed by item_info category string (worlds/cvaos/data/item_info) so callers can route a
# received item straight from its item_info.
INVENTORY: dict[str, InventoryArray] = {
    "consumable":   _byte_array("consumable",   0x02013294, 0x20),
    "weapon":       _byte_array("weapon",       0x020132B4, 0x3B),
    "armor":        _byte_array("armor",        0x020132EF, 0x19),
    "accessory":    _byte_array("accessory",    0x02013308, 0x14),
    "red_soul":     _soul_array("red_soul",     0x0201331C, 56),
    "blue_soul":    _soul_array("blue_soul",    0x02013354, 26),
    "yellow_soul":  _soul_array("yellow_soul",  0x0201336E, 36),
    "ability_soul": _soul_array("ability_soul", 0x02013392, 6),
}
# Money (subtype 1) is not an inventory array; it adds to CURRENT_GOLD.

# Index within the "consumable" array of the Skull Key -- the placeholder AoS grants Soma whenever he
# collects a pickup that belongs to another world (rom/patch.py ``_AP_PLACEHOLDER``). Its owned-count
# is meaningless to AP (the location check rides the pickup save flag, not the grant), but it shares
# the consumable cap of 9, so the client keeps it low to leave headroom for the next pickup.
SKULL_KEY_CONSUMABLE_INDEX = 25
