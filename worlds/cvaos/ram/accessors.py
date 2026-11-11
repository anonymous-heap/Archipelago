"""
Async helpers for reading/writing Aria of Sorrow live memory.

``AoSRAM`` wraps a transport backend (a ``backend.RamBackend``, such as ``BizHawkBackend``). Its
transport primitives are ``read``, ``write``, ``guarded_write`` and ``guarded_write_many``, one
round-trip each. Its semantic accessors work over the ``Entry`` declarations in ``addresses.py``.
An entry supplies the ``(offset, nbytes)`` a request needs and the codec that decodes or encodes
the bytes, so an accessor names *what* it touches and never restates an address, a size, or a
signedness.
"""
from __future__ import annotations

from typing import Any, NamedTuple, Sequence

from . import addresses as addr
from .._bytemaker_compat import Entry
from .addresses import EWRAM
from .backend import GuardedWrite, RamBackend
from .structures import SoulPair


class PlayState(NamedTuple):
    """The values the play-data log records, as :meth:`AoSRAM.read_play_data` returns them."""

    play_frames: int
    weapon: int   # the equipped weapon's id within its category
    armor: int    # the equipped armor's id within its category
    souls: frozenset[tuple[str, int]]  # every soul owned, as (item_info category, id)
    drops_seen: tuple[frozenset[int], frozenset[int]]  # enemy ids, for drop slot 1 and slot 2


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

    async def guarded_write_many(self, writes: Sequence[GuardedWrite]) -> bool:
        """
        Makes every write in ``writes``, or none of them if any target no longer holds its
        ``expected`` bytes.

        Returns False if any guard fails, and the caller retries next tick. An empty batch returns
        True without sending a request.
        """
        if not writes:
            return True
        return await self.backend.guarded_write_many(writes)

    # --- entry-shaped helpers -------------------------------------------------
    async def _fetch(self, entry: Entry, domain: str = EWRAM) -> Any:
        """
        Read and decode one entry. ``request()`` gives the offset and size, and ``parse()`` decodes
        the bytes.
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

    @staticmethod
    def _guarded(entry: Entry, new: bytes, expected: bytes, domain: str = EWRAM) -> GuardedWrite:
        """
        Build the guarded write :meth:`_cas_bytes` would send, as a ``GuardedWrite`` for a batch.
        """
        offset, _ = entry.request()
        return GuardedWrite(offset, bytes(new), bytes(expected), domain)

    async def read_group(self, *entries: Entry, domain: str = EWRAM) -> list[Any]:
        """
        Read several entries in ONE round-trip, and decode each with its own codec.
        """
        requests = [entry.request() for entry in entries]
        raw = await self.backend.read_many([(offset, size, domain) for offset, size in requests])
        return [entry.parse(data) for entry, data in zip(entries, raw)]

    # --- gameplay state -----------------------------------------------------
    async def get_run_state(self) -> tuple[int, int]:
        """
        Return ``(GAME_STATE, MENU_STATE)`` in a single round-trip.

        GAME_STATE is a ``GameState`` value (INGAME, GAME_OVER, ...). MENU_STATE is the in-room
        sub-state, one of the ``MENU_STATE_*`` values such as NORMAL, PAUSE or the death fade.
        The client tick reads both even when not in gameplay, and decides from them itself whether
        Soma is in normal gameplay.
        """
        state, menu = await self.read_group(addr.GAME_STATE, addr.MENU_STATE)
        return state, menu

    async def get_deathlink_state(self) -> tuple[int, int, int, bool]:
        """
        Return GAME_STATE, MENU_STATE, current HP and the kill-request flag, in one read.

        The DeathLink relay decides from these four values together, so they must come from the
        same frame. Separate reads can land on different frames, and the ROM hook can run a
        requested kill between them.

        HP is signed. The engine stores and tests it signed, and an overkill hit can leave it
        briefly negative before it is clamped to 0, so a ``> 0`` test must read it signed.
        """
        state, menu, hp, kill_request = await self.read_group(
            addr.GAME_STATE, addr.MENU_STATE, addr.CURRENT_HP, addr.KILL_REQUEST)
        return state, menu, hp, bool(kill_request)

    # --- hard mode and cleared data -----------------------------------------
    async def ensure_hard_mode(self) -> bool:
        """
        Force the difficulty nibble of the game-mode byte to Hard, preserving the character
        nibble. Returns True if a write was needed. The game writes this byte only at new game
        and mode select, so writing it each tick keeps it set. Damage scaling, soul-drop rates
        and the HARD_PICKUP spawn gate all read it live.
        """
        mode: int = await self._fetch(addr.GAME_MODE)
        if (mode & addr.GAME_MODE_DIFFICULTY_MASK) == addr.GAME_MODE_HARD:
            return False
        await self._store(addr.GAME_MODE, (mode & ~addr.GAME_MODE_DIFFICULTY_MASK & 0xFF) | addr.GAME_MODE_HARD)
        return True

    async def ensure_game_cleared(self) -> bool:
        """
        Mark the file as cleared-data (cutscenes become Start-skippable) by ORing the beaten value
        into the cleared flags. Returns True if a write was needed. The game writes these flags
        only on completion, so writing them each tick keeps them set. They are the low byte of the
        0x02000060 word.
        """
        flags: int = await self._fetch(addr.GAME_CLEARED_FLAGS)
        if (flags & addr.GAME_CLEARED_VALUE) == addr.GAME_CLEARED_VALUE:
            return False
        await self._store(addr.GAME_CLEARED_FLAGS, flags | addr.GAME_CLEARED_VALUE)
        return True

    # --- location detection -------------------------------------------------
    async def read_pickup_flags(self) -> bytes:
        return await self._fetch(addr.PICKUP_FLAGS)

    async def read_enemy_defeats(self) -> bytes:
        """Return the Enemy List's defeated bits: one per enemy type, set on its first defeat."""
        return await self._fetch(addr.ENEMY_DEFEATED)

    async def read_exploration(self) -> tuple[bytes, int, bytes]:
        """Return the map's explored-cell bitmasks, the first 32 event flags, and how many of
        each Castle Map Soma holds, in one read."""
        cells, event_flags, castle_maps = await self.read_group(
            addr.EXPLORED_MAP, addr.EVENT_FLAGS, addr.CASTLE_MAPS)
        return cells, event_flags, castle_maps

    @staticmethod
    def _flag_id(byte_index: int, bit: int) -> int:
        """
        The collected-pickup flag id for ``bit`` of byte ``byte_index``.

        This is the only definition of the bit numbering for the 0x02000360 bitfield. Ids are
        LSB-first within each byte, so byte 0 holds ids 0-7 and bit 0 is the 0x01 bit. The game
        tests a flag as ``unk_360[id >> 5] & (1 << (id & 0x1F))``, an LSB-first little-endian
        u32, which numbers the bits the same way. For MSB-first numbering, change this to
        ``byte_index * 8 + (7 - bit)``.
        """
        return byte_index * 8 + bit

    @staticmethod
    def defeated_enemy_ids(defeat_bytes: bytes) -> set[int]:
        """Return the ids of the enemy types whose defeated bit is set in ``defeat_bytes``.

        The Enemy List numbers its bits as the pickup flags do: bit ``id & 7`` of byte ``id >> 3``.
        """
        return AoSRAM.pickup_flag_ids(defeat_bytes)

    @staticmethod
    def pickup_flag_ids(flag_bytes: bytes) -> set[int]:
        """
        Return the collected-pickup flag ids whose bit is set in ``flag_bytes``.

        Each id is a pickup's ``flag_offset``, which the client maps to an AP location id.
        ``_flag_id`` defines the bit numbering.
        """
        ids: set[int] = set()
        for byte_index, value in enumerate(flag_bytes):
            for bit in range(8):
                if value & (1 << bit):
                    ids.add(AoSRAM._flag_id(byte_index, bit))
        return ids

    # --- player state -------------------------------------------------------
    async def get_player_room_position(self) -> tuple[int, int, int, int]:
        """Return Soma's area, room, and his x and y in pixels within the room, in one read."""
        area, room, camera_x, camera_y, screen_x, screen_y = await self.read_group(
            addr.CURRENT_AREA, addr.CURRENT_ROOM, addr.CAMERA_X, addr.CAMERA_Y,
            addr.PLAYER_SCREEN_X, addr.PLAYER_SCREEN_Y)
        return area, room, camera_x + screen_x, camera_y + screen_y

    async def get_max_hp(self) -> int:
        """
        Max HP, which is never 0 once a save file has loaded.

        The client tick reads it to tell a loaded file from one that is still loading.
        """
        return await self._fetch(addr.MAX_HP)

    async def read_play_data(self) -> PlayState:
        """
        Return the play timer, the equipped weapon and armor, the souls owned and the drop-seen
        bits, in one read.

        A soul counts as owned when its total over both of its lists is above 0, the total
        :meth:`owned_count` gives. The drop-seen fields number their bits like the defeated field.
        """
        arrays = [array for array in addr.INVENTORY.values() if array.nibble_packed]
        lists = [entry for array in arrays for entry in (array.entry, array.second)
                 if entry is not None]
        frames, gear, seen_1, seen_2, *values = await self.read_group(
            addr.PLAY_TIMER, addr.GEAR, addr.DROP_1_SEEN, addr.DROP_2_SEEN, *lists)
        values = iter(values)
        souls = set()
        for array in arrays:
            first = next(values)
            second = next(values) if array.second is not None else None
            for index in range(array.length):
                which = "odd" if index % 2 else "even"
                total = getattr(first[index // 2], which)
                if second is not None:
                    total += getattr(second[index // 2], which)
                if total:
                    souls.add((array.name, index))
        return PlayState(frames, gear.weapon, gear.armor, frozenset(souls),
                         (frozenset(self.defeated_enemy_ids(seen_1)),
                          frozenset(self.defeated_enemy_ids(seen_2))))

    # --- DeathLink ----------------------------------------------------------
    async def request_kill(self) -> None:
        """
        Applies an incoming DeathLink by asking the ROM hook to run the game's *real* death routine.

        This sets the kill-request flag. The hook (rom/deathlink_hook.py) checks the flag on every
        frame that updates Soma. If he is not already dying, the hook clears the flag and calls the
        death handler. Otherwise it leaves the flag set. HP regeneration cannot undo this kill.
        This method requires a ROM patched with the hook, which only DeathLink seeds have.
        """
        await self._store(addr.KILL_REQUEST, 1)

    async def clear_kill_request(self) -> None:
        """Clear the kill-request flag, so the ROM hook does not run the pending kill."""
        await self._store(addr.KILL_REQUEST, 0)

    # --- goal flags ---------------------------------------------------------
    async def get_boss_flags(self) -> int:
        return await self._fetch(addr.BOSS_FLAGS)

    async def get_global_flags(self) -> int:
        return await self._fetch(addr.GLOBAL_FLAGS)

    async def has_defeated(self, boss_flag: int) -> bool:
        """
        True when the boss flags have ``boss_flag`` set, one of the ``addr.BOSS_FLAG_*`` masks.
        """
        return bool(await self.get_boss_flags() & boss_flag)

    async def has_good_ending(self) -> bool:
        return bool(await self.get_global_flags() & addr.GLOBAL_FLAG_GOOD_ENDING)

    # --- received-items counter ---------------------------------------------
    async def get_received_count(self) -> int:
        return await self._fetch(addr.AP_RECEIVED_COUNT)

    async def set_received_count(self, count: int, expected: int) -> bool:
        """
        Advances the saved received-items counter to ``count`` if it still holds ``expected``.

        The counter sits in two saved bytes the game never uses (see ``addr.AP_RECEIVED_COUNT``).
        Only the client writes it, so the guard is only a precaution. Returns ``False`` only if
        the counter changed underneath us, in which case the caller re-reads and retries next
        tick instead of advancing.
        """
        return await self.guarded_write_many([self.received_count_write(count, expected)])

    def received_count_write(self, count: int, expected: int) -> GuardedWrite:
        """
        Builds the guarded write that :meth:`set_received_count` sends, without sending it.

        Pass it in a grant's ``together_with`` to advance the counter in the same step as the item.
        Either both change or neither does, so a failed request cannot leave the item granted and
        the counter behind it.
        """
        entry = addr.AP_RECEIVED_COUNT
        return self._guarded(entry, entry.pack(count & 0xFFFF), entry.pack(expected & 0xFFFF))

    # --- giving items -------------------------------------------------------
    @staticmethod
    def _check_index(category: str, array: addr.InventoryArray, index: int) -> None:
        if not 0 <= index < array.length:
            raise ValueError(f"{category} index {index} out of range (0..{array.length - 1})")

    async def owned_count(self, category: str, index: int) -> int:
        """How many of one item the player owns, from the same arrays :meth:`give_item` writes.

        ``category``/``index`` are an item_info category and its within-category id. Soul arrays
        pack two counts per byte, which this unpacks. A red, blue or yellow soul's count is the
        sum of both of its lists, as the game counts it.
        """
        array = addr.INVENTORY[category]
        self._check_index(category, array, index)
        if array.nibble_packed:
            slot = array.entry.item(index // 2)
            pair: SoulPair = await self._fetch(slot)
            return getattr(pair, "odd" if index % 2 else "even") + await self._second_count(
                array, index)
        return await self._fetch(array.entry.item(index))

    async def _second_count(self, array: addr.InventoryArray, index: int) -> int:
        """The count of soul ``index`` in the array's second list, or 0 when it has none."""
        if array.second is None:
            return 0
        pair: SoulPair = await self._fetch(array.second.item(index // 2))
        return getattr(pair, "odd" if index % 2 else "even")

    async def give_item(self, category: str, index: int, *, cap: int = 9,
                        together_with: Sequence[GuardedWrite] = ()) -> bool:
        """
        Increments the owned-count for one item, race-safely.

        ``category`` is an item_info category string (``"consumable"``,
        ``"weapon"``, ``"blue_soul"``, ...) and ``index`` is the item's id within
        that category (its ``item_info`` id, i.e. the EWRAM array index; this is NOT
        the pickup ``var_b`` for souls).

        AoS caps every owned-count at 9, the default ``cap``, for consumables, equipment and
        souls alike. The game refuses a consumable, weapon or armor pickup at the cap, so the
        pickup stays on the floor. The one exception this world adds is the Skull Key, which
        rom/skull_key_pickup.py lets the player collect at the cap without counting it.

        A received item has no pickup to leave behind, so this method drops a copy that would
        exceed the cap and logs it, rather than treating it as a lost race. Returns ``False``
        only if a guarded write lost a race, in which case nothing was written and the caller
        retries next tick.

        ``together_with`` holds other guarded writes to make in the same step as the item's own,
        so either all of them happen or none do. They are made even when the item is already at
        its cap, so a received-counter advance passed here still happens when the copy is dropped.

        A soul also adds one to the game's souls-collected statistic, which is what the vanilla
        collect path does after its own add. The first copy of an ability soul also switches the
        ability on, as the game's pickup does, because the player cannot use an ability while it
        is off.
        """
        array = addr.INVENTORY[category]
        self._check_index(category, array, index)

        if array.nibble_packed:
            # Two counts share the byte: read the pair, bump one nibble, and guard on the whole
            # byte so the neighbour's count is preserved. At the cap the byte is written back
            # unchanged, as in the byte-array case below.
            # The cap applies to the soul's total over both lists, and the soul goes into the
            # first list, as the game's own add does.
            slot = array.entry.item(index // 2)
            pair: SoulPair = await self._fetch(slot)
            old_byte = slot.pack(pair)
            which = "odd" if index % 2 else "even"
            current: int = getattr(pair, which)
            total = current + await self._second_count(array, index)
            at_cap = total >= cap
            if not at_cap:
                setattr(pair, which, current + 1)
            writes = [self._guarded(slot, slot.pack(pair), old_byte)]
            if category == "ability_soul" and total == 0:
                writes.append(await self._ability_switched_on(index))
            # The caller's writes go last. If a backend fails partway through the batch, the soul
            # can then be granted without the counter advancing, and it is granted again later.
            # With the counter first, a failure could instead count the soul as delivered while its
            # ability stays switched off.
            writes.extend(together_with)
            if not await self.guarded_write_many(writes):
                return False
            if at_cap:
                self._report_at_cap(category, index, cap)
            # Only souls reach here, and vanilla's collect path bumps the souls-collected
            # statistic after its add, so a granted soul counts the same way.
            await self._count_soul_collected()
            return True

        # At the cap the slot is written back unchanged, so the guard still catches a change made
        # between the read and the write (the player using one, say). The next tick then retries
        # the grant.
        slot = array.entry.item(index)
        current = await self._fetch(slot)
        new = min(current + 1, cap)
        if not await self.guarded_write_many(
                [self._guarded(slot, slot.pack(new), slot.pack(current)), *together_with]):
            return False
        if new == current:
            self._report_at_cap(category, index, cap)
        return True

    async def _ability_switched_on(self, index: int) -> GuardedWrite:
        """Build the guarded write that switches ability soul ``index`` on in the Ability menu."""
        flags = addr.ABILITY_ENABLED.item(index // 8)
        current: int = await self._fetch(flags)
        return self._guarded(flags, flags.pack(current | 1 << index % 8), flags.pack(current))

    async def _count_soul_collected(self) -> None:
        """Add one to the game's souls-collected statistic, the way a real pickup does.

        The vanilla collect path calls ``sub_08032DBC(1)`` after adding the soul, so a soul the
        client grants has to count here or the file screen undercounts. It counts souls absorbed
        rather than distinct souls owned, which is why an at-cap grant still counts.

        Best-effort on purpose: the soul and any ``together_with`` writes are already in memory by
        now, so losing this race must not fail the grant. A failed grant would skip the soul's
        announcement.
        """
        current: int = await self._fetch(addr.SOULS_COLLECTED)
        new = min(current + 1, addr.SOULS_COLLECTED_CAP)
        if new != current:
            await self._cas(addr.SOULS_COLLECTED, new, current)

    @staticmethod
    def _report_at_cap(category: str, index: int, cap: int) -> None:
        # Already at the cap, so a received copy has nowhere to go. Log it, since it is a real drop
        # and not a lost race. give_item still makes its together_with writes, so the
        # received-counter moves past it.
        from CommonClient import logger
        logger.warning("CVAoS: %s slot %d already at cap %d; received item dropped.",
                       category, index, cap)

    async def add_gold(self, amount: int, *, together_with: Sequence[GuardedWrite] = ()) -> bool:
        """
        Adds ``amount`` to the player's gold (subtype-1 'money' pickups),
        race-safely. The caller resolves the money item to its gold value. Like the game, it
        caps the total at ``addr.GOLD_CAP``.

        ``together_with`` works as in :meth:`give_item`.
        """
        entry = addr.CURRENT_GOLD
        current: int = await self._fetch(entry)
        new = max(current, min(current + amount, addr.GOLD_CAP))
        gold = self._guarded(entry, entry.pack(new), entry.pack(current))
        return await self.guarded_write_many([gold, *together_with])

    async def cap_skull_keys(self, *, limit: int = 1, floor: int = 0) -> None:
        """
        Clamp the Skull Key count into ``[floor, limit]``.

        Every pickup that belongs to another world grants Soma a Skull Key locally
        (rom/patch.py ``_AP_PLACEHOLDER``). The count means nothing to AP, since the location
        check comes from the pickup's save flag rather than the grant. The game refuses a
        consumable pickup while its count is 9, but rom/skull_key_pickup.py exempts the Skull
        Key. These pickups therefore stay collectable at any count, even with no client running.
        This method only keeps the junk count low, by lowering it to ``limit`` whenever it is
        higher.

        With the Skull Key warp feature on, the caller passes ``floor=1`` so the count never drops
        below 1 either, which keeps the warp item available. The write is unguarded: the value
        means nothing to AP, so a lost race is corrected on the next tick.
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
