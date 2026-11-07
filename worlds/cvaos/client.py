"""
BizHawk Client for Castlevania: Aria of Sorrow.

This client validates the patched ROM, authenticates, and connects. Its per-tick game logic
lives in the transport-neutral ``client_logic.CVAOSClientLogic`` mixin, which it shares with the
Collection Client (``collection_client.py``). That logic sends location checks, grants received
items, relays DeathLink and reports the goal. It also keeps Soma's position and the explored map
in data storage, where the map tracker reads them; Universal Tracker's map reads the position too.

This module supplies the parts specific to BizHawk: ROM validation over the connector, the
``BizHawkBackend``, and the connector's error handling. It also adds the /map command and the
"Open map" button, which open the map tracker for the connected slot in a browser.
"""
from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import worlds._bizhawk as bizhawk
from worlds._bizhawk.client import BizHawkClient

from .client_logic import CVAOSClientLogic, items_handling_for
from .options import TARGET_ADVANCE_COLLECTION
from .ram import AoSRAM, BizHawkBackend
from .rom import (ARCHIPELAGO_IDENTIFIER, ARCHIPELAGO_IDENTIFIER_START, AUTH_NUMBER_START,
                  COMPATIBLE_IDENTIFIERS)

if TYPE_CHECKING:
    from worlds._bizhawk.context import BizHawkClientCommandProcessor, BizHawkClientContext

# GBA header: 12-byte internal game title at 0xA0.
ROM_NAME_START = 0xA0
ROM_NAME = "CASTLEVANIA2"


def cmd_map(self: "BizHawkClientCommandProcessor") -> None:
    """Open the map tracker for this slot in your browser."""
    from .tracker_app import open_for_client
    open_for_client(self.ctx)


class CVAOSClient(CVAOSClientLogic, BizHawkClient):
    game = "Castlevania - Aria of Sorrow"
    system = "GBA"
    patch_suffix = ".apcvaos"
    # The auth of the seed the last session played. The BizHawk Client keeps one instance of this
    # class for as long as it runs, so the value survives a reconnect.
    _session_auth: str | None = None

    async def validate_rom(self, ctx: "BizHawkClientContext") -> bool:
        valid = await self._validate_rom(ctx)
        # /map and the "Open map" button are for Aria of Sorrow only, like other games' commands in
        # this client. A ROM that passes validation adds /map, and one that fails removes both. A
        # context with no command processor, such as a test's stand-in, gets no /map.
        commands = getattr(getattr(ctx, "command_processor", None), "commands", None)
        if commands is not None:
            if valid:
                commands["map"] = cmd_map
            else:
                commands.pop("map", None)
        if not valid:
            from .tracker_app import remove_map_button
            remove_map_button(ctx)
        return valid

    async def _validate_rom(self, ctx: "BizHawkClientContext") -> bool:
        from CommonClient import logger

        try:
            rom_name, identifier = await bizhawk.read(ctx.bizhawk_ctx, [
                (ROM_NAME_START, len(ROM_NAME), "ROM"),
                (ARCHIPELAGO_IDENTIFIER_START, len(ARCHIPELAGO_IDENTIFIER), "ROM"),
            ])
            # Reject anything that isn't Aria of Sorrow.
            if rom_name.decode("ascii", "ignore") != ROM_NAME:
                return False
            # Reject an unpatched ROM (the identifier region is still zeroed).
            if identifier == b"\x00" * len(ARCHIPELAGO_IDENTIFIER):
                logger.info("ERROR: This looks like an unpatched Aria of Sorrow ROM. Generate a "
                            "patch file and use it to create a patched ROM.")
                return False
            # Reject a ROM patched by an incompatible generator/client version.
            if identifier.decode("ascii", "ignore") not in COMPATIBLE_IDENTIFIERS:
                logger.info("ERROR: This ROM was patched by an incompatible apworld version. Use a "
                            "ROM patched from a seed generated with this apworld, or switch to "
                            "the apworld that generated the seed.")
                return False
        except (UnicodeDecodeError, bizhawk.RequestFailedError):
            return False

        ctx.game = self.game
        # Receive items from other worlds and the starting inventory. Nothing else delivers the
        # starting inventory to the game, while logic counts it as owned from the start. A seed from
        # V0.5 on also asks for this player's own items (client_logic.items_handling_for).
        ctx.items_handling = items_handling_for(identifier)
        ctx.want_slot_data = True
        ctx.watcher_timeout = 0.125
        return True

    async def set_auth(self, ctx: "BizHawkClientContext") -> None:
        auth_raw = (await bizhawk.read(ctx.bizhawk_ctx, [(AUTH_NUMBER_START, 16, "ROM")]))[0]
        ctx.auth = base64.b64encode(auth_raw).decode("ascii")
        # This runs on every connect to the server, including an automatic reconnect.
        self._reset_session_state(same_seed=ctx.auth == self._session_auth)
        self._session_auth = ctx.auth

    def on_package(self, ctx: "BizHawkClientContext", cmd: str, args: dict) -> None:
        if cmd == "Connected":
            from CommonClient import logger

            from .tracker_app import add_map_button
            add_map_button(ctx)  # adds it only once, though Connected arrives on every reconnect
            logger.info('Press "Open map" or type /map to open the map tracker for this slot in '
                        "your browser.")
            target = (args.get("slot_data") or {}).get("target_platform")
            if target is not None and int(target) == TARGET_ADVANCE_COLLECTION:
                logger.info(
                    "This seed's target platform is the Steam Advance Collection. You can play it "
                    "in BizHawk, but the intended client is the CVAoS Collection Client.")
        self.queue_deathlink_from_bounce(ctx, cmd, args)
        self.track_received_items(ctx, cmd, args)

    async def game_watcher(self, ctx: "BizHawkClientContext") -> None:
        if ctx.server is None or ctx.slot is None:
            return
        try:
            await self._tick(ctx, AoSRAM(BizHawkBackend(ctx.bizhawk_ctx)))
        except (bizhawk.RequestFailedError, bizhawk.NotConnectedError):
            # Emulator/connection hiccup; retry next tick. A request that times out closes the
            # connection to the connector, so the next request raises NotConnectedError. That must
            # not end the watcher either, because the context reconnects and calls it again.
            return
