"""
Async helpers for reading/writing Aria of Sorrow live memory.

``AoSRAM`` wraps a transport backend (``backend.RamBackend``; the BizHawk connector is one) with
three transport primitives (``read`` / ``write`` / ``guarded_write``, one round-trip each)
and semantic accessors over the ``Entry`` declarations in ``addresses.py``. An entry supplies the
``(offset, nbytes)`` a request needs and the codec that decodes or encodes the bytes, so an
accessor names *what* it touches and never restates an address, a size, or a signedness.
"""
from __future__ import annotations

from typing import Any, Sequence

from . import addresses as addr
from .._bytemaker_compat import Entry
from .addresses import EWRAM
from .backend import RamBackend
from .structures import EquippedGear, PlayerVitals, SoulPair


class AoSRAM:
    """
    Typed accessor over AoS live memory for one transport backend.
    """

    def __init__(self, backend: RamBackend) -> None:
        self.backend = backend

    # --- transport primitives (one backend round-trip each) -----------------
    async def read(self, offset: int, size: int, domain: str = EWRAM) -> bytes:
        return (await self.backend.read_many([(offset, size, domain)]))[0]

    async def write(self, offset: int, data: Sequence[int], domain: str = EWRAM) -> None:
        await self.backend.write(offset, data, domain)

    async def guarded_write(self, offset: int, data: Sequence[int], expected: Sequence[int],
                            domain: str = EWRAM) -> bool:
        """
        Writes ``data`` at ``offset`` only if the bytes there still equal ``expected``.

        Returns False if the guard failed (the value changed underneath us);
        the caller should retry next tick rather than advancing any counter.
        """
        return await self.backend.guarded_write(offset, data, expected, domain)

    # --- entry-shaped helpers -------------------------------------------------
    async def _fetch(self, entry: Entry, domain: str = EWRAM) -> Any:
        """
        Read and decode one entry: ``request()`` says where and how much, ``parse()`` what it means.
        """
        offset, size = entry.request()
        return entry.parse(await self.read(offset, size, domain))

    async def _store(self, entry: Entry, value: Any, domain: str = EWRAM) -> None:
        """
        Encode ``value`` in the entry's codec and write it at the entry's address.
        """
        offset, _ = entry.request()
        await self.write(offset, entry.pack(value), domain)

    async def _cas(self, entry: Entry, new: Any, expected: Any, domain: str = EWRAM) -> bool:
        """
        Guarded write with both sides stated as values: the entry packs the new bytes and the guard.
        """
        return await self._cas_bytes(entry, entry.pack(new), entry.pack(expected), domain)

    async def _cas_bytes(self, entry: Entry, new: bytes, expected: bytes, domain: str = EWRAM) -> bool:
        offset, _ = entry.request()
        return await self.guarded_write(offset, new, expected, domain)

    async def read_group(self, *entries: Entry, domain: str = EWRAM) -> list[Any]:
        """
        Several entries in ONE round-trip, each decoded by its own codec.
        """
        requests = [entry.request() for entry in entries]
        raw = await self.backend.read_many([(offset, size, domain) for offset, size in requests])
        return [entry.parse(data) for entry, data in zip(entries, raw)]

    # --- gameplay state -----------------------------------------------------
    async def get_run_state(self) -> tuple[int, int]:
        """
        ``(GAME_STATE, MENU_STATE)`` in a single round-trip. GAME_STATE is a
        ``GameState`` value (INGAME / GAME_OVER / ...); MENU_STATE is the in-room
        sub-state (NORMAL / ROOM_TRANSITION / PAUSE / SHOP, plus the death-fade
        value ``MENU_STATE_DEATH``). The DeathLink relay needs both even when not
        in gameplay, so it reads them here and derives ``is_in_gameplay`` itself.
        """
        state, menu = await self.read_group(addr.GAME_STATE, addr.MENU_STATE)
        return state, menu

    async def is_in_gameplay(self) -> bool:
        """
        True only when it is safe to read checks / inject items: normal in-room
        gameplay, not paused / transitioning / in a menu / game-over (sec. 5b).
        """
        game_state, menu_state = await self.get_run_state()
        return game_state == addr.GameState.INGAME and menu_state == addr.MENU_STATE_NORMAL

    # --- hard mode ----------------------------------------------------------
    async def ensure_hard_mode(self) -> bool:
        """
        Force the difficulty nibble of the game-mode byte to Hard, preserving the character
        nibble. Returns True if a write was needed. The game only writes this byte at
        new-game / mode-select, so re-applying it per tick holds without fighting the game;
        damage scaling, soul-drop rates, and the HARD_PICKUP spawn gate read it live.
        """
        mode: int = await self._fetch(addr.GAME_MODE)
        if (mode & addr.GAME_MODE_DIFFICULTY_MASK) == addr.GAME_MODE_HARD:
            return False
        await self._store(addr.GAME_MODE, (mode & ~addr.GAME_MODE_DIFFICULTY_MASK & 0xFF) | addr.GAME_MODE_HARD)
        return True

    async def ensure_game_cleared(self) -> bool:
        """
        Mark the file as cleared-data (cutscenes become Start-skippable) by ORing the cleared
        flags' low byte to the beaten value. Returns True if a write was needed. The game writes
        this only on game completion, so re-applying it per tick holds; the flags live in the low
        byte of the 0x02000060 word.
        """
        flags: int = await self._fetch(addr.GAME_CLEARED_FLAGS)
        if (flags & addr.GAME_CLEARED_VALUE) == addr.GAME_CLEARED_VALUE:
            return False
        await self._store(addr.GAME_CLEARED_FLAGS, flags | addr.GAME_CLEARED_VALUE)
        return True

    # --- location detection -------------------------------------------------
    async def read_pickup_flags(self) -> bytes:
        return await self._fetch(addr.PICKUP_FLAGS)

    @staticmethod
    def _flag_id(byte_index: int, bit: int) -> int:
        """
        The collected-pickup flag id for ``bit`` of byte ``byte_index`` of the 0x02000360
        bitfield -- the single home of the bit-numbering convention. LSB-first within each byte
        (byte 0 = ids 0-7, bit 0 = the 0x01 bit). The game tests these flags as
        ``unk_360[id >> 5] & (1 << (id & 0x1F))``, i.e.
        LSB-first little-endian u32 -- identical to this per-byte form. For MSB-first, change to
        ``byte_index * 8 + (7 - bit)``.
        """
        return byte_index * 8 + bit

    @staticmethod
    def pickup_flag_ids(flag_bytes: bytes) -> set[int]:
        """
        Set of collected-pickup flag ids whose bit is set. Maps each id through
        ``flag_offset -> ptr_address`` to get AP location ids; bit-numbering lives in ``_flag_id``.
        """
        ids: set[int] = set()
        for byte_index, value in enumerate(flag_bytes):
            for bit in range(8):
                if value & (1 << bit):
                    ids.add(AoSRAM._flag_id(byte_index, bit))
        return ids

    # --- typed structs ------------------------------------------
    async def get_vitals(self) -> PlayerVitals:
        """
        The full HP/MP block, typed (``.current_hp`` etc. are plain ints).
        """
        return await self._fetch(addr.VITALS)

    async def get_equipped_gear(self) -> EquippedGear:
        """
        Currently-equipped weapon/souls/armor/accessory indices, typed.
        """
        return await self._fetch(addr.GEAR)

    async def get_current_hp(self) -> int:
        """
        Current HP as a SIGNED s16 -- the engine stores and tests it signed, and an
        overkill hit can leave it briefly negative before being clamped to 0, so a
        ``> 0`` test (used to confirm a real respawn) must read it signed. The signedness
        comes from the ``current_hp`` field's declaration. See `get_vitals` for the whole
        typed block.
        """
        return await self._fetch(addr.CURRENT_HP)

    async def get_max_hp(self) -> int:
        """
        Lean single-field read for the DeathLink poll. See `get_vitals` for the
        whole typed block.
        """
        return await self._fetch(addr.MAX_HP)

    async def request_kill(self) -> None:
        """
        Applies an incoming DeathLink by asking the ROM hook to run the game's *real* death routine:
        set the kill-request flag; the hook (rom/deathlink_hook.py) calls the death handler the next
        frame and clears the flag. Deterministic and regen-proof, unlike ``kill_player``. Requires a
        ROM patched with the hook (DeathLink-enabled seeds, identifier >= CVAOS_AP_V0.2).
        """
        await self._store(addr.KILL_REQUEST, 1)

    async def get_kill_request(self) -> bool:
        """True while a DeathLink kill-request is still pending (the ROM hook hasn't consumed it yet)."""
        return bool(await self._fetch(addr.KILL_REQUEST))

    async def kill_player(self) -> None:
        """
        Legacy DeathLink kill: zero current HP and let the engine's per-frame HP check notice. Racy
        (HP regen can cancel it; skipped in some states). Kept as a fallback; ``request_kill`` is the
        current path for hook-patched ROMs.
        """
        await self._store(addr.CURRENT_HP, 0)

    # --- goal flags ---------------------------------------------------------
    async def get_boss_flags(self) -> int:
        return await self._fetch(addr.BOSS_FLAGS)

    async def get_global_flags(self) -> int:
        return await self._fetch(addr.GLOBAL_FLAGS)

    async def has_defeated(self, boss_flag: int) -> bool:
        """
        ``boss_flag`` is one of the ``addr.BOSS_FLAG_*`` bit masks.
        """
        return bool(await self.get_boss_flags() & boss_flag)

    async def has_good_ending(self) -> bool:
        return bool(await self.get_global_flags() & addr.GLOBAL_FLAG_GOOD_ENDING)

    async def get_received_count(self) -> int:
        return await self._fetch(addr.AP_RECEIVED_COUNT)

    async def set_received_count(self, count: int, expected: int) -> bool:
        """
        Advances the saved received-items counter to ``count``, guarded on its prior value
        ``expected`` so the client never blind-stomps the word. The counter lives on a verified-dead
        saved byte that only the client writes (see addr.AP_RECEIVED_COUNT), so the guard is
        belt-and-suspenders: returns ``False`` only if the word changed underneath us, in which case
        the caller re-reads and retries next tick instead of advancing.
        """
        return await self._cas(addr.AP_RECEIVED_COUNT, count & 0xFFFF, expected & 0xFFFF)

    # --- giving items -------------------------------------------------------
    @staticmethod
    def _check_index(category: str, array: addr.InventoryArray, index: int) -> None:
        if not 0 <= index < array.length:
            raise ValueError(f"{category} index {index} out of range (0..{array.length - 1})")

    async def owned_count(self, category: str, index: int) -> int:
        """How many of one item the player owns, from the same arrays :meth:`give_item` writes.

        ``category``/``index`` are an item_info category and its within-category id. Soul arrays
        pack two counts per byte, which this unpacks.
        """
        array = addr.INVENTORY[category]
        self._check_index(category, array, index)
        if array.nibble_packed:
            slot = array.entry.item(index // 2)
            pair: SoulPair = await self._fetch(slot)
            return getattr(pair, "odd" if index % 2 else "even")
        return await self._fetch(array.entry.item(index))

    async def give_item(self, category: str, index: int, *, cap: int = 9) -> bool:
        """
        Increments the owned-count for one item, race-safely.

        ``category`` is an item_info category string (``"consumable"``,
        ``"weapon"``, ``"blue_soul"``, ...) and ``index`` is the item's id within
        that category (its ``item_info`` id, i.e. the EWRAM array index; this is NOT
        the pickup ``var_b`` for souls). AoS caps every owned-count -- consumables,
        equipment, and souls alike -- at ``cap`` (9); the game accepts a pickup at the
        cap but won't count it past 9, and we mirror that. A received item that would
        exceed the cap is dropped and logged (still returns ``True`` so the counter
        advances). Returns ``True`` on success/at-cap, or ``False`` if the guarded write
        lost a race -- retry next tick and do **not** advance the received counter.

        A soul also adds one to the game's souls-collected statistic, which is what the vanilla
        collect path does after its own add.
        """
        array = addr.INVENTORY[category]
        self._check_index(category, array, index)

        if array.nibble_packed:
            # Two counts share the byte: read the pair, bump one nibble, and guard on the whole
            # byte so the neighbour's count rides along untouched.
            slot = array.entry.item(index // 2)
            pair: SoulPair = await self._fetch(slot)
            old_byte = slot.pack(pair)
            which = "odd" if index % 2 else "even"
            current: int = getattr(pair, which)
            new = min(current + 1, cap)
            if new == current:
                self._report_at_cap(category, index, cap)
            else:
                setattr(pair, which, new)
                if not await self._cas_bytes(slot, slot.pack(pair), old_byte):
                    return False
            # Only souls reach here, and vanilla's collect path bumps the souls-collected
            # statistic after its add, so a granted soul counts the same way.
            await self._count_soul_collected()
            return True

        slot = array.entry.item(index)
        current = await self._fetch(slot)
        new = min(current + 1, cap)
        if new == current:
            self._report_at_cap(category, index, cap)
            return True
        return await self._cas(slot, new, current)

    async def _count_soul_collected(self) -> None:
        """Add one to the game's souls-collected statistic, the way a real pickup does.

        The vanilla collect path calls ``sub_08032DBC(1)`` after adding the soul, so a soul the
        client grants has to count here or the file screen undercounts. It counts souls absorbed
        rather than distinct souls owned, which is why an at-cap grant still counts.

        Best-effort on purpose: the soul is already in the inventory by now, so losing this race
        must not fail the grant (the retry would hand out the soul twice).
        """
        current: int = await self._fetch(addr.SOULS_COLLECTED)
        new = min(current + 1, addr.SOULS_COLLECTED_CAP)
        if new != current:
            await self._cas(addr.SOULS_COLLECTED, new, current)

    @staticmethod
    def _report_at_cap(category: str, index: int, cap: int) -> None:
        # Already at the cap: AoS accepts the pickup but won't count it past 9, so a received copy
        # has nowhere to go. Log it (a real drop, not a race) rather than swallow it silently; the
        # caller then reports success so the received-counter still advances.
        from CommonClient import logger
        logger.warning("CVAoS: %s slot %d already at cap %d; received item dropped.",
                       category, index, cap)

    async def add_gold(self, amount: int) -> bool:
        """
        Adds ``amount`` to the player's gold (subtype-1 'money' pickups),
        race-safely. The caller resolves the money item to its gold value.
        """
        current: int = await self._fetch(addr.CURRENT_GOLD)
        return await self._cas(addr.CURRENT_GOLD, min(current + amount, 0xFFFFFFFF), current)

    async def cap_skull_keys(self, *, limit: int = 1, floor: int = 0) -> None:
        """
        Clamp the Skull Key count into ``[floor, limit]``, race-safely.

        Every pickup that belongs to another world grants Soma a Skull Key locally
        (rom/patch.py ``_AP_PLACEHOLDER``); the count is junk to AP, since the location check
        rides the pickup save flag, not the grant. But AoS caps every consumable at 9 and won't
        count a pickup past the cap, so a player who lets Skull Keys pile up could stop registering
        pickups -- and stop sending other worlds their items. So we knock the count back down to
        ``limit`` whenever it climbs above, always leaving headroom to collect the next pickup.

        With the Skull Key warp feature on, the caller passes ``floor=1`` so the count never drops
        below 1 either -- keeping the warp item always available. The write is unguarded: the value
        is meaningless to AP, so a lost race just self-corrects next tick.
        """
        slot = addr.INVENTORY["consumable"].entry.item(addr.SKULL_KEY_CONSUMABLE_INDEX)
        current: int = await self._fetch(slot)
        target = min(max(current, floor), limit)
        if target != current:
            await self._store(slot, target)

    async def set_flag_bit(self, offset: int, bit: int, value: int) -> bool:
        """
        Sets (``value`` truthy) or clears bit ``bit`` (0-7) of the EWRAM byte at ``offset``,
        race-safely. Returns ``True`` on success or no-op, ``False`` if the guarded write lost a
        race (retry next tick without advancing the received-counter).

        ``offset`` is an EWRAM-domain offset computed by the caller (a save-flag byte), so this
        one accessor works on the raw byte rather than on a declared entry.
        """
        current = (await self.read(offset, 1))[0]
        mask = 1 << bit
        new = current | mask if value else current & ~mask & 0xFF
        if new == current:
            return True
        return await self.guarded_write(offset, [new], [current])
