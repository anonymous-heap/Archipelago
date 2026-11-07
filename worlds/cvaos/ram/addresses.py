"""
Aria of Sorrow live-memory map (EWRAM) for the clients.

Every location is an ``Entry`` on the EWRAM address plane, declared by its full GBA address.
BizHawk's ``"EWRAM"`` memory domain takes offsets from the EWRAM base ``0x02000000``, and
``entry.request()`` returns exactly that ``(offset, nbytes)`` pair, so the subtraction lives in
the ``Space`` rather than beside every constant. Fields of the two record blocks
(``PlayerVitals``, ``EquippedGear``) are addressed through their layout, so the per-field
entries below cannot drift from the structs.

Values (state codes, bit masks, indices) stay plain ints. Every address was verified against
the USA ROM.

``ROM`` names the other memory domain the clients read, whose offsets are ROM file offsets.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .._bytemaker_compat import Buffer, Entry, Space, count, s16, u8, u16, u32
from .structures import EquippedGear, PlayerVitals, SoulPair

# --- Memory/BizHawk address-section names ---
EWRAM = "EWRAM"
ROM = "ROM"  # offsets in this domain are ROM file offsets

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


# Game modes, named after the decomp's GAME_MODE_* enum (constants/main.h), which also leaves
# 0x03 unnamed.
class GameState(IntEnum):
    KONAMI_LOGO = 0x00
    TITLE = 0x01
    MAIN_MENU = 0x02
    INGAME = 0x04
    GAME_OVER = 0x05
    CREDITS = 0x06
    INTRO_CUTSCENE = 0x07


# Saving does not leave MENU_STATE_NORMAL, so the save prompt and the save write happen while the
# client may still grant items.
MENU_STATE_NORMAL = 0x01            # in-room, not transitioning/paused/shopping
MENU_STATE_DEATH = 0x02             # death/game-over fade sub-state (drives DeathLink detection)
MENU_STATE_ROOM_TRANSITION = 0x03
MENU_STATE_MAP_SCREEN = 0x04        # the Select map (observed live)
MENU_STATE_PAUSE = 0x06
MENU_STATE_WARP_MAP = 0x07          # warp-room map screen (observed live in the collection)
MENU_STATE_ITEM_WARP = 0x08         # a Skull Key warp from the pause menu (observed live)
MENU_STATE_SHOP = 0x09


# --- Location detection: collected-pickup save flags ---
# 0x02000360, 20 bytes / 160 bits. A byte blob, so it decodes straight to ``bytes``.
PICKUP_FLAGS = ewram.entry(0x02000360, Buffer.of(nbytes=0x14), name="pickup_flags")
PICKUP_FLAGS_LEN = PICKUP_FLAGS.size

# --- Location detection: the Enemy List's defeated bits (enemy checks) ---
# 0x020133A0, 16 bytes: one bit per enemy type, bit ``id & 7`` of byte ``id >> 3``. The game's
# death routine sets a type's bit the first time any enemy of that type is defeated, and the save
# keeps it.
ENEMY_DEFEATED = ewram.entry(0x020133A0, Buffer.of(nbytes=0x10), name="enemy_defeated")
# The Enemy List's drop-seen bits, one field per drop slot, numbered like ENEMY_DEFEATED. When a
# slot's item drops, the death routine sets that type's bit in the slot's field (0x080685F8),
# whether or not Soma collects the item. The save keeps both fields.
DROP_1_SEEN = ewram.entry(0x020133B0, Buffer.of(nbytes=0x10), name="drop_1_seen")
DROP_2_SEEN = ewram.entry(0x020133C0, Buffer.of(nbytes=0x10), name="drop_2_seen")


# --- Progress / goal flags ---
# The first 32 of the game's event flags (the decomp's unk_60.unk_33C), one bit each, LSB first.
# Flags 0 to 12 record the hidden doors Soma has gone through, in map_info.hidden_doors order.
EVENT_FLAGS = ewram.entry(0x0200033C, u32, name="event_flags")
# The map's record of where Soma has been (the decomp's unk_60.unk_B4[2][40]). Entry [half][row]
# is two u32 bitmasks over columns half*32 to half*32 + 31, with bit 0 the leftmost. The first
# marks the cells visited, which the map draws in full, and the second marks the cells remembered,
# which it draws dimmed. See exploration.py.
EXPLORED_MAP = ewram.entry(0x020000B4, Buffer.of(nbytes=0x280), name="explored_map")
# How many of each Castle Map Soma holds: consumables 29 to 31 (the decomp's itemInventory[0x1D]
# to [0x1F]), one byte each. Each one held reveals areas on the map (exploration.CASTLE_MAP_AREAS).
CASTLE_MAPS = ewram.entry(0x020132B1, Buffer.of(nbytes=3), name="castle_maps")
BOSS_FLAGS = ewram.entry(0x0200037E, u16, name="boss_flags")
GLOBAL_FLAGS = ewram.entry(0x0200042C, u32, name="global_flags")
CURRENT_AREA = ewram.entry(0x0200009E, u8, name="current_area")
CURRENT_ROOM = ewram.entry(0x0200009F, u8, name="current_room")
# Soma's position in the room is the camera's position (background layer 1's scroll) plus his
# position on screen, as GetEntityRoomXPositionInteger computes it. PLAYER_SCREEN_X and _Y are
# fields of the first entity, which is Soma.
CAMERA_X = ewram.entry(0x0200A09A, u16, name="camera_x")
CAMERA_Y = ewram.entry(0x0200A09E, u16, name="camera_y")
PLAYER_SCREEN_X = ewram.entry(0x02000526, s16, name="player_screen_x")
PLAYER_SCREEN_Y = ewram.entry(0x0200052A, s16, name="player_screen_y")
CURRENT_SAVE_SLOT = ewram.entry(0x02000428, u8, name="current_save_slot")

BOSS_FLAG_GRAHAM = 0x0001          # bad-ending final boss
BOSS_FLAG_DEATH = 0x0002
BOSS_FLAG_CHAOS = 0x0080           # true-ending final boss; does not persist, so the goal reads
                                   # GLOBAL_FLAG_GOOD_ENDING instead
GLOBAL_FLAG_GOOD_ENDING = 0x00004000

# --- Play timer ---
# The decomp's unk_60.inGameTimer, saved with the file. UpdateInGameTimer (0x0800CADC) adds one
# per frame of play and stops at 0x014996C4, about 100 hours at 60 to a second.
PLAY_TIMER = ewram.entry(0x020000AC, u32, name="play_timer")


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
# The game clamps gold to 999,999 wherever it adds some (sub_08044F80 and the money-pickup path).
GOLD_CAP = 999_999

# totalNbrSoulsCollected: the running count the file-select and status screens show. The game
# counts souls ABSORBED, not distinct souls owned, and clamps at SOULS_COLLECTED_CAP.
SOULS_COLLECTED = ewram.entry(0x02013264, u16, name="souls_collected")
SOULS_COLLECTED_CAP = 999

# The number of received items given so far, which only the client writes. It lives in pad_1328A,
# two bytes the game never uses (verified), which are saved with the file and zeroed on a new game.
AP_RECEIVED_COUNT = ewram.entry(0x0201328A, u16, name="ap_received_count")

# DeathLink kill-request flag. The client writes 1, and the ROM hook (rom/deathlink_hook.py)
# clears it when it runs the game's real death routine. It lives in pad_1324C. Live testing in mGBA
# showed that the engine never writes that byte during combat, menus, the shop, saving or loading.
KILL_REQUEST = ewram.entry(0x0201324C, u8, name="kill_request")


@dataclass(frozen=True)
class InventoryArray:
    """
    One of AoS's owned-item count arrays: an ``Entry`` over its bytes plus the item scheme.

    Byte arrays hold one count per byte. Soul arrays nibble-pack two souls per byte, one
    ``SoulPair`` record each (low nibble = even index, high nibble = odd). ``entry.item(i)``
    does the base + index arithmetic either way.

    Red, blue and yellow souls also have a second list, ``second``, laid out the same way. The
    file-select Copy moves a file's souls into it. The game's total for a soul is the sum of both
    lists (SoulInventory_GetSoulTotal). Ability souls have no second list.
    """

    name: str
    entry: Entry
    length: int          # number of items the array can address
    nibble_packed: bool
    second: Entry | None = None


def _byte_array(name: str, gba_addr: int, items: int) -> InventoryArray:
    return InventoryArray(name, ewram.entry(gba_addr, u8, count(items), name=name), items, False)


def _soul_array(name: str, gba_addr: int, items: int,
                second_addr: int | None = None) -> InventoryArray:
    pairs = (items + 1) // 2
    second = (None if second_addr is None
              else ewram.entry(second_addr, SoulPair, count(pairs), name=f"{name}_second"))
    return InventoryArray(name, ewram.entry(gba_addr, SoulPair, count(pairs), name=name), items,
                          True, second)


# Keyed by item_info category string (worlds/cvaos/data/item_info) so callers can route a
# received item straight from its item_info.
INVENTORY: dict[str, InventoryArray] = {
    "consumable":   _byte_array("consumable",   0x02013294, 0x20),
    "weapon":       _byte_array("weapon",       0x020132B4, 0x3B),
    "armor":        _byte_array("armor",        0x020132EF, 0x19),
    "accessory":    _byte_array("accessory",    0x02013308, 0x14),
    "red_soul":     _soul_array("red_soul",     0x0201331C, 56, second_addr=0x02013338),
    "blue_soul":    _soul_array("blue_soul",    0x02013354, 26, second_addr=0x02013361),
    "yellow_soul":  _soul_array("yellow_soul",  0x0201336E, 36, second_addr=0x02013380),
    "ability_soul": _soul_array("ability_soul", 0x02013392, 6),
}
# Money (subtype 1) is not an inventory array; it adds to CURRENT_GOLD.

# The Ability menu's On/Off switch for each ability soul: bit i of byte i // 8 is soul i. The
# player code checks a soul's bit before using its ability (sub_08032AB8), and the game's pickup
# sets the bit when it adds a soul's first copy (sub_08032ADC).
ABILITY_ENABLED = ewram.entry(0x02013396, u8, count(2), name="ability_enabled")

# Index of the Skull Key within the "consumable" array. Collecting a pickup that belongs to another
# world gives Soma a Skull Key as a placeholder (rom/patch.py ``_AP_PLACEHOLDER``). Its owned-count
# means nothing to AP, because the location check comes from the pickup's save flag rather than the
# grant. rom/skull_key_pickup.py lets a Skull Key pickup be collected at the cap of 9, and the
# client also keeps the count low (AoSRAM.cap_skull_keys).
SKULL_KEY_CONSUMABLE_INDEX = 25
