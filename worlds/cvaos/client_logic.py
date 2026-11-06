"""
Transport-neutral per-tick client logic for Castlevania: Aria of Sorrow.

``CVAOSClientLogic`` is the game logic that both clients share: the BizHawk Client
(``client.py``) and the Collection Client (``collection_client.py``). It holds the per-session
state, and its ``_tick`` runs one private helper per task. Those helpers send location checks,
grant received items and Progressive Movement souls, relay DeathLink, and report the goal. They
also keep Soma's position and the explored map in data storage, where the map tracker reads
them; Universal Tracker's map reads the position too.

The logic reaches the game only through ``AoSRAM``, on whichever transport backend the caller
chose, and reaches Archipelago only through ``CommonContext``. It therefore works the same in
either client. Each client wraps ``_tick`` in error handling for its own backend, and retries or
reconnects when that backend fails.

``CVAOSClient`` mixes this class in, so tests can call helpers such as
``client._relay_deathlink`` directly. The Collection Client's context holds an instance instead.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from NetUtils import ClientStatus

from . import exploration, item_granting, tracker_map
from .data import item_info_collection
from .enemy_checks import defeat_location_id, enemy_id_for_location
from .items import PROGRESSIVE_MOVEMENT, PROGRESSIVE_MOVEMENT_STAGES, item_table
from .locations import flag_offset_to_location_id
from .options import Goal
from .ram import AoSRAM, addresses as addr
from .rom.patch import OWN_ITEMS_FROM_SERVER_IDENTIFIER

if TYPE_CHECKING:
    from CommonClient import CommonContext

PROGRESSIVE_MOVEMENT_CODE = item_table[PROGRESSIVE_MOVEMENT].code
STARTING_LOCATION = -2  # the location id the server gives starting-inventory items
_STAGE_INFOS = tuple(next(info for info in item_info_collection if info.name == name)
                     for name in PROGRESSIVE_MOVEMENT_STAGES)

# Soma's position for the trackers' maps is sent when it changes, at most this often.
POSITION_INTERVAL = 0.5
# The explored map is read this often, and sent when it has changed.
EXPLORATION_INTERVAL = 2.0

# The items_handling a client connects with. Both ask for items from other worlds and for the
# starting inventory. A seed with enemy checks also asks for the player's own items, because the
# ROM never hands out the item at a defeat location (see items_handling_for).
ITEMS_FROM_OTHER_WORLDS = 0b101
ITEMS_INCLUDING_OWN = 0b111


def items_handling_for(identifier: bytes) -> int:
    """Return the items_handling for a ROM that carries ``identifier``."""
    if identifier == OWN_ITEMS_FROM_SERVER_IDENTIFIER.encode("ascii"):
        return ITEMS_INCLUDING_OWN
    return ITEMS_FROM_OTHER_WORLDS


# Max items to grant per tick, so a large backlog on connect can't stall the watcher
# (each grant is a few emulator round-trips). Leftovers are picked up on the following ticks.
MAX_ITEMS_PER_TICK = 10


class CVAOSClientLogic:
    """Per-session state and per-tick game logic, shared by both clients."""

    # Per-session state, (re)initialized in _reset_session_state.
    death_causes: list[str]
    local_checked_locations: set[int]
    currently_dead: bool | None
    time_of_sent_death: float | None
    goaled: bool

    def _reset_session_state(self, *, same_seed: bool = False) -> None:
        # Reset per-session state so swapping ROMs/games without restarting the client can't
        # carry anything stale over. Each client calls this on every connect or attach, with
        # same_seed True when it is reconnecting to the seed it was already playing.
        self.local_checked_locations = set()
        # DeathLink: the cause of an incoming death not applied yet. A reconnect to the same seed
        # keeps it, so a death received while Soma could not be killed (paused, say) is still
        # applied.
        if not same_seed:
            self.death_causes = []
        # DeathLink: whether we currently consider Soma dead. It guards against re-sending our own
        # death and re-applying an incoming one until HP recovers. A reconnect to the same seed
        # keeps it, so a death sent before the disconnect is not sent again, while one that
        # happened during the disconnect still is. On a first connect or a different seed, it is
        # None until the first tick reads the game.
        if not same_seed:
            self.currently_dead = None
        # AP's timestamp on the death we last sent, so the bounce handler can recognize that death
        # when the server bounces it back.
        self.time_of_sent_death = None
        # Goal: latched True once we've reported completion, so we report it only once.
        self.goaled = False
        # The position and the map cell last sent, and when the position was sent, so a new
        # session sends both again.
        self.last_position = None
        self.last_cell = None
        self.last_position_time = 0.0
        # The explored map last sent, and when it was last read.
        self.last_exploration = None
        self.last_exploration_time = 0.0

    def queue_deathlink_from_bounce(self, ctx: "CommonContext", cmd: str, args: dict) -> None:
        # Queue an incoming DeathLink's cause; _relay_deathlink applies the kill. Skip the echo of
        # our own death (it carries the timestamp we recorded when we sent it) so we never re-kill
        # Soma from our own death after he respawns.
        if cmd != "Bounced" or "tags" not in args or ctx.slot is None:
            return
        if "DeathLink" in args["tags"]:
            data = args.get("data", {})
            if data.get("time") == self.time_of_sent_death:
                return  # our own death, bounced back to us
            if self.currently_dead:
                return  # Soma is already dead or dying, so this death changes nothing
            # Keep one pending death: several that arrive while Soma cannot be killed yet (paused,
            # in a menu) kill him once, not once each after every respawn.
            source = data.get("source", "Another world")
            self.death_causes = [data.get("cause") or f"{source} killed you without a word!"]

    async def _tick(self, ctx: "CommonContext", ram: AoSRAM) -> None:
        # Read the run state and relay DeathLink every tick. The work that needs normal gameplay
        # (checks, tracker data, items, goal) runs only when it is safe to read and write game
        # memory.
        game_state, menu_state = await ram.get_run_state()
        in_gameplay = (game_state == addr.GameState.INGAME
                       and menu_state == addr.MENU_STATE_NORMAL)
        # DeathLink runs even outside normal gameplay, because a death takes the game out of it
        # (the death fade, then the game-over screen). Within normal play, HP is 0 for only about
        # one frame, so a 0.125s poll limited to normal play would almost always miss the death.
        # The relay reads the run state again itself, in the same request as HP and the
        # kill-request flag.
        await self._relay_deathlink(ctx, ram)
        # Keep these values forced in any in-game sub-state, so they are already set when a room
        # transition loads its entities. The game writes them only at new game, mode select and
        # completion, so writing them each tick keeps them set.
        if game_state == addr.GameState.INGAME:
            # Mark every randomized ROM as cleared-data so cutscenes are Start-skippable.
            await ram.ensure_game_cleared()
            # Hard Mode: force the difficulty nibble so HARD_PICKUP entities spawn.
            if ctx.slot_data.get("hard_mode"):
                await ram.ensure_hard_mode()
        # Reading checks and granting items is safe only in normal in-room gameplay (not paused,
        # transitioning, in a menu, dying, or at game over), so we never read garbage or write at
        # an unsafe time.
        #
        # Normal gameplay is not quite enough on its own. The pickup flags, the inventory and the
        # received-counter all live in the save-data block, which reads as zeros while a file is
        # still loading, even though GAME_STATE and MENU_STATE already report normal play
        # (observed live). A grant made then lands in memory the load is about to overwrite. If
        # the load overwrites the advanced counter, the item is given again next tick. Max HP sits
        # in the same block and is never 0 for a loaded file, so it tells the two apart.
        if in_gameplay and await ram.get_max_hp() > 0:
            await self._send_location_checks(ctx, ram)
            await self._report_position(ctx, ram)
            await self._report_exploration(ctx, ram)
            await self._receive_items(ctx, ram)
            await self._grant_progressive_movement(ctx, ram)
            await self._report_goal(ctx, ram)

    async def _report_position(self, ctx: "CommonContext", ram: AoSRAM) -> None:
        # Keep Soma's room and position in data storage for the trackers' maps. The tracker's map
        # reads POSITION_KEY, which follows him as he moves. Universal Tracker's map reads
        # MAP_CELL_KEY, which changes only when he enters another cell, because UT marks only the
        # cell and reloads its map whenever the key changes.
        value = tracker_map.position_value(*await ram.get_player_room_position())
        if value == self.last_position:
            return
        now = time.monotonic()
        if now - self.last_position_time < POSITION_INTERVAL:
            return
        self.last_position, self.last_position_time = value, now
        keys = [tracker_map.POSITION_KEY]
        cell = tracker_map.cell_of(value)
        if cell != self.last_cell:
            self.last_cell = cell
            keys.append(tracker_map.MAP_CELL_KEY)
        await ctx.send_msgs([{"cmd": "Set", "key": key.format(team=ctx.team, player=ctx.slot),
                              "default": None, "want_reply": False,
                              "operations": [{"operation": "replace", "value": value}]}
                             for key in keys])

    async def _report_exploration(self, ctx: "CommonContext", ram: AoSRAM) -> None:
        # Keep the explored map in data storage, where the tracker reads which parts of the castle
        # to show (exploration.py). It changes only when Soma enters a new cell, goes through a
        # hidden door or gets a Castle Map, so it is read every few seconds rather than every tick.
        now = time.monotonic()
        if now - self.last_exploration_time < EXPLORATION_INTERVAL:
            return
        self.last_exploration_time = now
        value = exploration.value(*await ram.read_exploration())
        if value == self.last_exploration:
            return
        self.last_exploration = value
        key = exploration.EXPLORATION_KEY.format(team=ctx.team, player=ctx.slot)
        await ctx.send_msgs([{"cmd": "Set", "key": key, "default": None, "want_reply": False,
                              "operations": [{"operation": "replace", "value": value}]}])

    async def _send_location_checks(self, ctx: "CommonContext", ram: AoSRAM) -> None:
        # Collected-pickup save flags -> AP location checks. Each set bit's index is a pickup's
        # flag_offset. We map it to the AP location id and send it if the server tracks that
        # location for this slot.
        flag_bytes = await ram.read_pickup_flags()
        checked: set[int] = set()
        for flag_offset in AoSRAM.pickup_flag_ids(flag_bytes):
            location_id = flag_offset_to_location_id.get(flag_offset)
            if location_id is not None and location_id in ctx.server_locations:
                checked.add(location_id)

        # The first defeat of each kind of enemy, in a seed with enemy checks.
        if any(enemy_id_for_location(location) is not None for location in ctx.server_locations):
            for enemy_id in AoSRAM.defeated_enemy_ids(await ram.read_enemy_defeats()):
                location_id = defeat_location_id(enemy_id)
                if location_id in ctx.server_locations:
                    checked.add(location_id)

        new_checks = checked - self.local_checked_locations
        if new_checks:
            self.local_checked_locations |= new_checks
            await ctx.send_msgs([{"cmd": "LocationChecks", "locations": sorted(new_checks)}])

        # Collecting another world's pickup grants Soma a junk Skull Key (rom/patch.py
        # _AP_PLACEHOLDER). The game refuses a consumable pickup while its count is 9, but
        # rom/skull_key_pickup.py exempts the Skull Key, so these pickups stay collectable at any
        # count, even offline. Lowering the count each tick only keeps the junk keys from piling
        # up. With the Skull Key Warp option on, the count is also kept at 1 or more, so the warp
        # item is always available. The cap only lowers the count and the floor only raises it, so
        # they never conflict.
        await ram.cap_skull_keys(floor=1 if ctx.slot_data.get("skull_key_warp") else 0)

    async def _receive_items(self, ctx: "CommonContext", ram: AoSRAM) -> None:
        # Receive items from the server: grant each not-yet-given item and advance the saved
        # received-counter past it, so a reload doesn't re-give it. An item's transfer and the
        # counter's advance go in one batch of guarded writes, so a connector failure can neither
        # grant an item twice nor skip one. Stop on a lost guarded write (a race with the game's
        # writes, or the counter changing underneath us) and retry next tick.
        from CommonClient import logger

        items = self._grantable_items(ctx)
        received = await ram.get_received_count()
        # Defensive: a counter ahead of everything the server has ever sent could only come from a
        # corrupt save; clamp so we never index past the items (a real grant rewrites it).
        received = min(received, len(items))
        granted_this_tick = 0
        while received < len(items) and granted_this_tick < MAX_ITEMS_PER_TICK:
            code = items[received].item
            action = item_granting.resolve(code)
            if action is None:
                # Unreachable today (every item code has a defined category); log instead of
                # silently dropping a received item if an undefined one is ever added to the pool.
                logger.warning("CVAoS: received item code %s has no grant mapping; skipping.", code)
                done = await ram.set_received_count(received + 1, expected=received)
            else:
                done = await item_granting.grant(
                    ram, action, new_soul_pause=bool(ctx.slot_data.get("new_soul_pause", True)),
                    together_with=[ram.received_count_write(received + 1, expected=received)])
            if not done:
                break  # lost a guarded write, so the counter was not advanced; retry next tick
            received += 1
            granted_this_tick += 1

    async def _grant_progressive_movement(self, ctx: "CommonContext", ram: AoSRAM) -> None:
        # Grant one stage soul per Progressive Movement copy obtained so far. The count adds the
        # copies in the starting inventory, the copies received from other worlds, and the copies
        # found at this world's own checked locations. Souls already owned are skipped, so repeating
        # this every tick is safe. When the player reloads an older save, it also restores the
        # souls that save lacks. Only one soul is granted per tick, and only once the announcement
        # slot is free, so that every granted soul gets its own banner.
        from . import announce
        if not ctx.slot_data.get("progressive_movement"):
            return
        starting = ctx.slot_data.get("progressive_movement_starting", 0)
        # Starting items arrive with location -2 when the client asks for them; slot_data already
        # counts those, so they are left out here.
        received = sum(1 for item in ctx.items_received
                       if item.item == PROGRESSIVE_MOVEMENT_CODE
                       and item.location != STARTING_LOCATION)
        checked = ctx.checked_locations | self.local_checked_locations
        found = sum(1 for location in ctx.slot_data.get("progressive_movement_locations", ())
                    if location in checked)
        if ctx.items_handling & 0b010:
            # The server sends this world's own copies too, so they are already in ``received``.
            found = 0
        for info in _STAGE_INFOS[:starting + received + found]:
            if await ram.owned_count(info.item_category, info.id) > 0:
                continue
            if not await announce.is_ready(ram):
                return
            if await ram.give_item(info.item_category, info.id):
                await announce.announce_item_number(
                    ram, info.item_number,
                    new_soul_pause=bool(ctx.slot_data.get("new_soul_pause", True)))
            return

    @staticmethod
    def _grantable_items(ctx: "CommonContext") -> list:
        """
        Return the received items that the saved received-counter counts, in server order.

        The server puts the starting inventory at the front of the list. A seed whose slot data
        has ``starting_inventory_from_server`` counts those items in its saves. An older seed's
        saves count only the items from other worlds. For such a seed this leaves the starting
        inventory out, so the client does not give it.

        A seed with enemy checks asks the server for the player's own items too. The ones at the
        slot data's ``rom_given_locations`` were already handed out by the ROM, so they are left
        out. The rest, such as an own item at a defeat location, are given like any other.
        """
        items = ctx.items_received
        if ctx.items_handling & 0b010:
            rom_given = set(ctx.slot_data.get("rom_given_locations", ()))
            items = [item for item in items
                     if not (item.player == ctx.slot and item.location in rom_given)]
        if ctx.slot_data.get("starting_inventory_from_server"):
            return items
        return [item for item in items if item.location != STARTING_LOCATION]

    async def _relay_deathlink(self, ctx: "CommonContext", ram: AoSRAM) -> None:
        # DeathLink: follow the setting in slot_data, and broadcast Soma's death as soon as the
        # game enters its death sub-state or the game-over screen. Apply an incoming death during
        # gameplay by asking the ROM hook to run the real death routine (request_kill). Once Soma
        # is alive again, re-arm.
        #
        # Detection reads the death STATE, not HP == 0. Every death from damage (combat, spikes, an
        # overkill laser) goes through one damage routine that clamps HP to exactly 0. In the same
        # frame, the player-death handler sets MENU_STATE to 2, and MENU_STATE keeps that value
        # through the multi-frame fade until GAME_STATE changes to GAME_OVER. That state lasts many
        # frames, so a 0.125s poll reliably sees it, while HP == 0 lasts about one frame in normal
        # gameplay.
        from CommonClient import logger

        # Follow the seed's DeathLink setting. update_death_link adds or removes the "DeathLink"
        # tag, so once the tag matches the setting this does nothing. Removing the tag matters when
        # the client moves on to a seed without DeathLink, which also has no kill hook to run a
        # requested death.
        wanted = bool(ctx.slot_data.get("death_link"))
        if wanted != ("DeathLink" in ctx.tags):
            await ctx.update_death_link(wanted)

        # Read the run state, HP and the kill-request flag in one request, so every decision below
        # sees the same frame. Separate reads can land on different frames, and the ROM hook can
        # run a requested kill between them. Normal play read before that kill, together with a
        # clear flag read after it, would re-arm the latch. The next tick would then broadcast the
        # kill as Soma's own death.
        game_state, menu_state, current_hp, kill_request = await ram.get_deathlink_state()
        in_gameplay = (game_state == addr.GameState.INGAME
                       and menu_state == addr.MENU_STATE_NORMAL)

        # Dead/dying: the in-game death fade (MENU_STATE==DEATH while still INGAME) or the game-over
        # screen. The sub-state is gated on INGAME so we never misread the byte outside gameplay.
        dead = ((game_state == addr.GameState.INGAME and menu_state == addr.MENU_STATE_DEATH)
                or game_state == addr.GameState.GAME_OVER)

        # When the latch is unknown (a first connect, or a different seed), set it from the game's
        # current state. A death already on screen began before the client connected, and a kill
        # request the ROM hook has not run yet is another world's death. Neither should be
        # broadcast as a new death of Soma's. If the latch starts set, a death queued before this
        # read is dropped, just as the bounce handler drops one that arrives while Soma is dead.
        if self.currently_dead is None:
            self.currently_dead = dead or kill_request
            if self.currently_dead:
                self.death_causes.clear()

        # Re-arm first: once Soma is alive again in normal play and no kill is still pending, clear
        # the latch so the next death in either direction is detected. The pending-kill guard
        # covers the frames between requesting an incoming death and the ROM hook running it
        # (state still NORMAL, flag still set), so we don't re-arm and then immediately
        # re-broadcast that same death.
        if self.currently_dead and in_gameplay and current_hp > 0 and not kill_request:
            self.currently_dead = False

        # The ROM hook skips a requested kill while Soma is already dying, and leaves the flag set.
        # The flag is outside the save data, so loading a save does not clear it. The hook would
        # then kill Soma again as soon as he is back in play. A received death kills him only once,
        # and he is already dead, so clear the flag.
        if dead and kill_request:
            await ram.clear_kill_request()

        if "DeathLink" in ctx.tags and dead and not self.currently_dead:
            # Died on our own. Mark dead BEFORE awaiting so a same-tick re-entry can't double-send.
            # A death still pending from another world is discarded, because Soma is already dead.
            self.currently_dead = True
            self.death_causes.clear()
            await ctx.send_death(f"{ctx.player_names[ctx.slot]} was slain. Dracula has won!")
            # Record AP's timestamp so the bounce handler can skip the echo of this death. It is
            # copied because ctx.last_death_link changes when another world's death arrives.
            self.time_of_sent_death = ctx.last_death_link

        if self.death_causes and not self.currently_dead and in_gameplay:
            # Apply an incoming death, whose cause the bounce handler queued. This happens only in
            # normal gameplay, because requesting the kill during the fade or a menu is unsafe. The
            # currently_dead check keeps us from killing Soma again, or re-broadcasting, for a
            # death already in progress.
            cause = self.death_causes.pop(0)
            await ram.request_kill()
            self.currently_dead = True
            logger.info("CVAoS DeathLink: %s", cause)

    async def _report_goal(self, ctx: "CommonContext", ram: AoSRAM) -> None:
        """
        Report seed completion when the player wins their chosen Goal.

        The graham goal is won once Graham is defeated (``BOSS_FLAGS & 0x01``). The chaos goal is
        won once Chaos's second phase is defeated, which the client reads from the good-ending
        flag (``GLOBAL_FLAGS & 0x4000``). The Chaos phase 2 bit (``BOSS_FLAGS & 0x80``) does not
        persist, so it cannot be used.
        """
        from CommonClient import logger

        if ctx.finished_game or self.goaled:
            return
        goal = ctx.slot_data.get("goal", None)
        if goal is None:
            logger.warning("No goal specified in slot data; cannot report goal completion.")
            return  # no goal, never report
        elif goal not in (Goal.option_graham, Goal.option_chaos):
            # Unrecognized goal: only from corrupt slot_data or a newer generator adding goals this
            # client can't check; log instead of silently dropping it.
            logger.warning("Unrecognized goal: %s", goal)
            return  # unrecognized goal, never report

        won = False
        if goal == Goal.option_graham:
            won = await ram.has_defeated(addr.BOSS_FLAG_GRAHAM)
        else:  # goal == Goal.option_chaos
            won = await ram.has_good_ending()
        if not won:
            return
        self.goaled = True
        ctx.finished_game = True
        await ctx.send_msgs([{"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}])
