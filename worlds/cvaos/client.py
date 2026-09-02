"""
BizHawk client for Castlevania: Aria of Sorrow.

This client validates the patched ROM, authenticates, and connects. The per-tick game
logic (location checks, item grants, DeathLink relay, goal report) lives in the
transport-neutral ``client_logic.CVAOSClientLogic`` mixin, shared with the Steam
Advance Collection client (``collection_client.py``); this module supplies only the
BizHawk-specific transport shell: ROM validation over the connector, the
``BizHawkBackend``, and the connector's error handling.
"""
from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import worlds._bizhawk as bizhawk
from worlds._bizhawk.client import BizHawkClient

from .client_logic import CVAOSClientLogic
from .options import TARGET_ADVANCE_COLLECTION
from .ram import AoSRAM, BizHawkBackend
from .rom import ARCHIPELAGO_IDENTIFIER, ARCHIPELAGO_IDENTIFIER_START, AUTH_NUMBER_START

if TYPE_CHECKING:
    from worlds._bizhawk.context import BizHawkClientContext

# GBA header: 12-byte internal game title at 0xA0.
ROM_NAME_START = 0xA0
ROM_NAME = "CASTLEVANIA2"


class CVAOSClient(CVAOSClientLogic, BizHawkClient):
    game = "Castlevania - Aria of Sorrow"
    system = "GBA"
    patch_suffix = ".apcvaos"

    async def validate_rom(self, ctx: "BizHawkClientContext") -> bool:
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
            if identifier.decode("ascii", "ignore") != ARCHIPELAGO_IDENTIFIER:
                logger.info("ERROR: This ROM was patched by an incompatible version. Check your "
                            "client version against the one used to generate the seed.")
                return False
        except (UnicodeDecodeError, bizhawk.RequestFailedError):
            return False

        ctx.game = self.game
        ctx.items_handling = 0b001  # receive items from other worlds
        ctx.want_slot_data = True
        ctx.watcher_timeout = 0.125
        return True

    async def set_auth(self, ctx: "BizHawkClientContext") -> None:
        auth_raw = (await bizhawk.read(ctx.bizhawk_ctx, [(AUTH_NUMBER_START, 16, "ROM")]))[0]
        ctx.auth = base64.b64encode(auth_raw).decode("ascii")
        self._reset_session_state()

    def on_package(self, ctx: "BizHawkClientContext", cmd: str, args: dict) -> None:
        if cmd == "Connected":
            target = (args.get("slot_data") or {}).get("target_platform")
            if target is not None and int(target) == TARGET_ADVANCE_COLLECTION:
                from CommonClient import logger
                logger.info(
                    "This seed's target platform is the Steam Advance Collection. You can play it "
                    "in BizHawk, but the intended client is the CVAoS Collection Client.")
        self.queue_deathlink_from_bounce(ctx, cmd, args)

    async def game_watcher(self, ctx: "BizHawkClientContext") -> None:
        if ctx.server is None or ctx.slot is None:
            return
        try:
            await self._tick(ctx, AoSRAM(BizHawkBackend(ctx.bizhawk_ctx)))
        except bizhawk.RequestFailedError:
            # Emulator/connection hiccup; retry next tick.
            return
