"""
Steam Advance Collection client for Castlevania: Aria of Sorrow.

The Castlevania Advance Collection (Steam AppID 1552550, developer M2) runs AoS inside
M2's proprietary emulator with no connector of any kind, so this client attaches to the
``game.exe`` process directly (pymem / ReadProcessMemory) and backs the ``RamBackend``
primitives with raw process-memory access. Everything game-side is the same
``client_logic.CVAOSClientLogic`` brain the BizHawk client uses; only the transport
differs.

Anchoring (finding the emulated GBA memory inside game.exe's address space). This was
tuned against the live process:

* ROM image: located by validating a real GBA header -- the "CASTLEVANIA2" title at +0xA0
  backed by the Nintendo logo at +0x04 and the "A2CE" game code at +0xAC. That uniquely
  picks the one true ROM out of the ~188 stray copies of the title string in memory.
  We then read ARCHIPELAGO_IDENTIFIER at ROM+0x670000 to confirm the ROM is patched and
  version-matched (a bare header = unpatched -> point at cac_archive.py; a different
  version suffix = stale install). The ROM sits in its own committed private-RW region.
* EWRAM: M2 heap-allocates the emulated GBA memory (no static pointer to it exists in
  game.exe), so we identify the live 256 KiB EWRAM by a strong in-game content signature:
  GAME_STATE==INGAME and MENU_STATE==NORMAL, self-consistent vitals (1<=HP<=MaxHP<=2000,
  MP<=MaxMP), a sane area id and gold, and every consumable count 0..9 -- all holding at
  the exact EWRAM offsets simultaneously. Live testing showed this resolves to exactly one
  block once the ROM region is excluded (ROM data can coincidentally match). The emulator's
  GBA-memory struct places EWRAM 0x170 into its host allocation, so we check region_base +
  0x170 first (near-instant) and fall back to a full 0x10-stride scan only if that misses.
  Because the signature requires normal gameplay, discovery only succeeds while the player
  is in a room -- which is also the only time the client acts, so it simply waits otherwise.
  If more than one block matches (the collection's rewind buffer can hold flat snapshots),
  the live one is the block whose low memory mutates between two reads; snapshots are frozen.
  The content signature alone is not proof: on the live process, frozen look-alike buffers
  (HP 3/3, every count 3) pass it while the player sits in a menu, so a match is accepted
  only if it is struct-aligned or its low memory moves.

Failure handling: every pymem/OS error is wrapped in ``CollectionError``; the watcher
drops to "detached", keeps the AP connection up, and re-attaches/re-anchors on a 5 s
cadence (the anchor is also revalidated every couple of seconds, because switching games
inside the collection or resetting can silently reallocate the emulated memory).
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import time
from typing import TYPE_CHECKING, NamedTuple, Sequence

import Utils

from ._bytemaker_compat import Entry
from .client_logic import CVAOSClientLogic
from .options import TARGET_ADVANCE_COLLECTION
from .ram import AoSRAM, addresses as addr
from .ram.addresses import EWRAM
from .rom import ARCHIPELAGO_IDENTIFIER, ARCHIPELAGO_IDENTIFIER_START, AUTH_NUMBER_START

if TYPE_CHECKING:
    import pymem

PROCESS_NAME = "game.exe"  # all four collection titles run under this one exe
INSTALL_DIR_FRAGMENT = "castlevania advance collection"  # main-module path must contain this

ROM_DOMAIN = "ROM"
EWRAM_SIZE = 0x40000
ROM_SIZE = 0x800000
ROM_TITLE_OFFSET = 0xA0
ROM_TITLE = b"CASTLEVANIA2"

# The patched ROM carries ARCHIPELAGO_IDENTIFIER at ROM+ARCHIPELAGO_IDENTIFIER_START. The
# collection client requires a patched ROM, so once the ROM image is located by its header
# we read the marker there to confirm patched/version.
_IDENTIFIER_BYTES = ARCHIPELAGO_IDENTIFIER.encode("ascii")
_IDENTIFIER_PREFIX = _IDENTIFIER_BYTES[:9]  # b"CVAOS_AP_" -- shared across patch versions

# GBA ROM header validation: the fixed Nintendo logo starts at ROM+0x04, and Aria of Sorrow
# (USA) has game code "A2CE" at 0xAC. Title + logo + code uniquely identify the true ROM
# image among the many stray copies of the "CASTLEVANIA2" string in process memory.
_ROM_LOGO_OFFSET = 0x04
_ROM_LOGO_PREFIX = bytes.fromhex("24ffae5169")  # first 5 bytes of the GBA logo
_ROM_GAMECODE_OFFSET = 0xAC
_ROM_GAMECODE = b"A2CE"

# In-game EWRAM signature bounds. Loose enough never to reject a real save, tight enough to
# exclude essentially all random memory (verified against the live process).
_MAX_VITAL = 2000      # AoS HP/MP never approach this
_MAX_GOLD = 500_000
_MAX_AREA = 0x2F



def ewram_offset(entry: Entry) -> int:
    """EWRAM-domain byte offset of an ``addresses`` entry (the map declares GBA addresses)."""
    return entry.request().offset


# The signature's fields as plain offsets into a candidate buffer.
_GAME_STATE = ewram_offset(addr.GAME_STATE)
_MENU_STATE = ewram_offset(addr.MENU_STATE)
_CURRENT_HP, _MAX_HP = ewram_offset(addr.CURRENT_HP), ewram_offset(addr.MAX_HP)
_CURRENT_MP, _MAX_MP = ewram_offset(addr.CURRENT_MP), ewram_offset(addr.MAX_MP)
_CURRENT_AREA = ewram_offset(addr.CURRENT_AREA)
_CURRENT_GOLD = ewram_offset(addr.CURRENT_GOLD)
# The signature reads EWRAM up to the end of the consumable count array; a candidate buffer
# must cover at least this many bytes past the base.
_CONSUM_BASE = ewram_offset(addr.INVENTORY["consumable"].entry)
_CONSUM_END = _CONSUM_BASE + addr.INVENTORY["consumable"].length
_SIG_SPAN = _CONSUM_END

# The emulator's GBA-memory struct places its EWRAM member 0x170 into the host allocation
# (observed on the only shipped build). Checking region_base+0x170 first makes discovery
# near-instant; a full 0x10-stride scan is the correctness fallback if that ever misses.
_EWRAM_STRUCT_OFFSET = 0x170

ATTACH_RETRY_SECONDS = 5.0
REVALIDATE_SECONDS = 2.0
TICK_SECONDS = 0.125
ERROR_BACKOFF_SECONDS = 5.0  # pause after an unexpected tick error before ticking again


class CollectionError(Exception):
    """Transport failure talking to game.exe (process gone, read/write refused, anchor lost)."""


# ---------------------------------------------------------------------------
# Win32 region enumeration (VirtualQueryEx) -- the one piece pymem doesn't wrap nicely.
# ---------------------------------------------------------------------------

class _Region(NamedTuple):
    base: int
    size: int
    protect: int
    state: int
    mem_type: int

    @property
    def readable(self) -> bool:
        # Committed, readable, and not guarded. PAGE_* read family: 0x02 READONLY,
        # 0x04 READWRITE, 0x20 EXECUTE_READ, 0x40 EXECUTE_READWRITE.
        return (self.state == 0x1000 and not self.protect & 0x100
                and bool(self.protect & (0x02 | 0x04 | 0x20 | 0x40)))

    @property
    def writable_private(self) -> bool:
        # MEM_PRIVATE committed RW -- where a heap-allocated EWRAM buffer must live.
        return (self.state == 0x1000 and self.mem_type == 0x20000
                and not self.protect & 0x100 and bool(self.protect & (0x04 | 0x40)))


class _MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_size_t),
        ("AllocationBase", ctypes.c_size_t),
        ("AllocationProtect", ctypes.c_ulong),
        ("PartitionId", ctypes.c_ushort),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.c_ulong),
        ("Protect", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
    ]


def _iter_regions(process_handle: int):
    """Walk the target's committed regions. game.exe is 32-bit, so user space ends < 4 GiB."""
    kernel32 = ctypes.windll.kernel32
    mbi = _MEMORY_BASIC_INFORMATION()
    address = 0
    while address < 0x1_0000_0000:
        if not kernel32.VirtualQueryEx(process_handle, ctypes.c_void_p(address),
                                       ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        yield _Region(mbi.BaseAddress, mbi.RegionSize, mbi.Protect, mbi.State, mbi.Type)
        address = mbi.BaseAddress + mbi.RegionSize


def _main_module_path(process_handle: int) -> str:
    buf = ctypes.create_unicode_buffer(1024)
    # None module handle = the main executable.
    if not ctypes.windll.psapi.GetModuleFileNameExW(process_handle, None, buf, len(buf)):
        return ""
    return buf.value


# ---------------------------------------------------------------------------
# Process wrapper
# ---------------------------------------------------------------------------

class GameProcess:
    """One attached game.exe: raw reads/writes plus region enumeration, all errors
    normalized to ``CollectionError``."""

    def __init__(self, pm: "pymem.Pymem", exe_path: str) -> None:
        self.pm = pm
        self.exe_path = exe_path

    @classmethod
    def attach(cls) -> "GameProcess":
        # pymem import is deferred so generation/tests never need it (Windows-only dep).
        try:
            import pymem
            import pymem.exception
        except ImportError as exc:
            raise CollectionError(
                "pymem is not installed. Install it (pip install Pymem) or run from a "
                "source install so ModuleUpdate picks up worlds/cvaos/requirements.txt.") from exc
        try:
            pm = pymem.Pymem(PROCESS_NAME)
        except pymem.exception.ProcessNotFound as exc:
            raise CollectionError(
                f"{PROCESS_NAME} is not running. Launch the Castlevania Advance Collection "
                "and load Aria of Sorrow.") from exc
        except pymem.exception.PymemError as exc:
            raise CollectionError(f"Could not attach to {PROCESS_NAME}: {exc}") from exc
        path = _main_module_path(pm.process_handle)
        if INSTALL_DIR_FRAGMENT not in path.lower():
            pm.close_process()
            raise CollectionError(
                f"Found a {PROCESS_NAME} that is not the Castlevania Advance Collection "
                f"({path or 'unknown path'}); is another game named game.exe running?")
        return cls(pm, path)

    def close(self) -> None:
        try:
            self.pm.close_process()
        except Exception:
            pass  # already gone; nothing to release

    def read(self, address: int, size: int) -> bytes:
        import pymem.exception
        try:
            return self.pm.read_bytes(address, size)
        except pymem.exception.PymemError as exc:
            raise CollectionError(f"read of {size:#x} bytes at {address:#x} failed: {exc}") from exc

    def write(self, address: int, data: bytes) -> None:
        import pymem.exception
        try:
            self.pm.write_bytes(address, bytes(data), len(data))
        except pymem.exception.PymemError as exc:
            raise CollectionError(f"write of {len(data):#x} bytes at {address:#x} failed: {exc}") from exc

    def regions(self) -> list[_Region]:
        return list(_iter_regions(self.pm.process_handle))


# ---------------------------------------------------------------------------
# Anchor discovery. The buffer-level helpers are pure so tests can drive them
# with synthetic bytes (test/test_collection_backend.py).
# ---------------------------------------------------------------------------

class RomImage(NamedTuple):
    base: int          # host address of ROM file offset 0
    region_start: int  # containing region bounds, used to exclude ROM memory from the EWRAM scan
    region_end: int


def validated_rom_base_in(buf: bytes, region_base: int) -> int | None:
    """Host base of a byte-valid AoS ROM header in ``buf`` (title + Nintendo logo +
    "A2CE" game code), or None. Pure/testable."""
    idx = buf.find(ROM_TITLE)
    while idx != -1:
        rom_off = idx - ROM_TITLE_OFFSET
        if (rom_off >= 0
                and buf[rom_off + _ROM_LOGO_OFFSET:rom_off + _ROM_LOGO_OFFSET + len(_ROM_LOGO_PREFIX)]
                == _ROM_LOGO_PREFIX
                and buf[rom_off + _ROM_GAMECODE_OFFSET:rom_off + _ROM_GAMECODE_OFFSET + len(_ROM_GAMECODE)]
                == _ROM_GAMECODE):
            return region_base + rom_off
        idx = buf.find(ROM_TITLE, idx + 1)
    return None


def find_rom_image(proc: GameProcess) -> RomImage:
    """Locate the loaded AoS ROM by validating its GBA header, then confirm it is patched
    with a compatible AP identifier. Raises with an actionable message otherwise."""
    for region in proc.regions():
        if not region.readable or region.size < 0x10000:
            continue
        try:
            buf = proc.read(region.base, region.size)
        except CollectionError:
            continue  # region vanished mid-scan; harmless
        rom_base = validated_rom_base_in(buf, region.base)
        if rom_base is None:
            continue
        # Found the one true ROM image. Confirm it carries our patch (the client can only
        # play a patched ROM: it reads the identifier/auth and the game data expects the hooks).
        marker = proc.read(rom_base + ARCHIPELAGO_IDENTIFIER_START, len(_IDENTIFIER_BYTES))
        if marker == _IDENTIFIER_BYTES:
            return RomImage(rom_base, region.base, region.base + region.size)
        if marker.startswith(_IDENTIFIER_PREFIX):
            ver = marker.split(b"\x00", 1)[0].decode("ascii", "replace")
            raise CollectionError(
                f"The collection's AoS ROM was patched by an incompatible version ({ver}; "
                f"this client needs {ARCHIPELAGO_IDENTIFIER}). Re-install a ROM patched for "
                f"this version.")
        raise CollectionError(
            "Aria of Sorrow is loaded but its ROM is unpatched. Apply your .apcvaos to the "
            "AC-extracted ROM and install it with advance_collection/cac_archive.py (see "
            "the setup guide).")
    raise CollectionError(
        "No Aria of Sorrow ROM found in the collection's memory. Select Aria of Sorrow in "
        "the collection first (the ROM is only loaded once a game is chosen).")


def ewram_signature_ok(buf: bytes, off: int) -> bool:
    """True if ``buf`` at ``off`` looks like live AoS EWRAM in normal gameplay: the exact
    in-room state plus self-consistent vitals, sane area/gold, and valid inventory counts.
    Pure/testable; the load-bearing discriminator for EWRAM discovery (see module docstring)."""
    if off < 0 or off + _SIG_SPAN > len(buf):
        return False
    if buf[off + _GAME_STATE] != int(addr.GameState.INGAME):
        return False
    if buf[off + _MENU_STATE] != addr.MENU_STATE_NORMAL:
        return False

    def u16(field: int) -> int:
        return int.from_bytes(buf[off + field:off + field + 2], "little")

    hp, maxhp = u16(_CURRENT_HP), u16(_MAX_HP)
    mp, maxmp = u16(_CURRENT_MP), u16(_MAX_MP)
    if not (1 <= maxhp <= _MAX_VITAL and 1 <= hp <= maxhp):
        return False
    if not (1 <= maxmp <= _MAX_VITAL and 0 <= mp <= maxmp):
        return False
    if buf[off + _CURRENT_AREA] > _MAX_AREA:
        return False
    if int.from_bytes(buf[off + _CURRENT_GOLD:off + _CURRENT_GOLD + 4], "little") > _MAX_GOLD:
        return False
    if any(b > 9 for b in buf[off + _CONSUM_BASE:off + _CONSUM_END]):
        return False
    return True


def _ewram_scan_regions(proc: GameProcess, rom: RomImage):
    """Private-RW regions big enough to hold EWRAM, excluding the ROM's own region (ROM data
    can coincidentally satisfy the signature)."""
    for region in proc.regions():
        if not region.writable_private or region.size < EWRAM_SIZE:
            continue
        if region.base < rom.region_end and region.base + region.size > rom.region_start:
            continue
        yield region


def find_ewram_base(proc: GameProcess, rom: RomImage) -> int:
    """Locate live AoS EWRAM. Fast path checks each candidate region at the emulator's
    struct offset (0x170); full 0x10-stride scan is the fallback. Excludes the ROM region.
    Disambiguates multiple hits (rewind snapshots) by frame mutation."""
    regions = list(_ewram_scan_regions(proc, rom))

    candidates: list[int] = []
    for region in regions:
        try:
            head = proc.read(region.base, _EWRAM_STRUCT_OFFSET + _SIG_SPAN)
        except CollectionError:
            continue
        if ewram_signature_ok(head, _EWRAM_STRUCT_OFFSET):
            candidates.append(region.base + _EWRAM_STRUCT_OFFSET)

    if not candidates:
        # Struct offset didn't match (unexpected build/state) -- full stride scan.
        for region in regions:
            try:
                buf = proc.read(region.base, region.size)
            except CollectionError:
                continue
            for scan_off in range(0, len(buf) - _SIG_SPAN + 1, 0x10):
                if ewram_signature_ok(buf, scan_off):
                    candidates.append(region.base + scan_off)

    if not candidates:
        raise CollectionError(
            "Aria of Sorrow is loaded but not in normal gameplay yet -- enter a room (not a "
            "menu, map, or pause screen) so the client can locate game memory. Still trying.")
    live = _live_candidates(proc, candidates)
    if not live:
        raise CollectionError(
            "Aria of Sorrow's memory signature matched only frozen look-alike buffers, not the "
            "running game -- enter a room and keep playing. Still trying.")
    return live[0]


def _is_struct_aligned(candidate: int) -> bool:
    # The emulator's struct puts EWRAM at region_base+0x170, and region bases are
    # page-aligned, so the live base's low 12 bits are 0x170.
    return (candidate & 0xFFF) == _EWRAM_STRUCT_OFFSET


def _live_candidates(proc: GameProcess, candidates: list[int]) -> list[int]:
    """The candidates that can be the running game's EWRAM, most likely first.

    A lone struct-aligned match is taken as-is. Otherwise each candidate's low memory is read
    twice a few frames apart: the live buffer mutates every frame during play, while rewind
    snapshots and look-alike buffers are frozen. Order: aligned and moving, then moving, then
    aligned but frozen (a paused game). Frozen unaligned matches are dropped; on the real
    process such blocks pass the content signature and would anchor the client on garbage.
    """
    aligned = [c for c in candidates if _is_struct_aligned(c)]
    if len(candidates) == 1 and aligned:
        return aligned
    first: dict[int, bytes | None] = {}
    for c in candidates:
        try:
            first[c] = proc.read(c, 0x2000)
        except CollectionError:
            first[c] = None
    time.sleep(0.15)  # a few frames of live play
    moving: list[int] = []
    for c in candidates:
        try:
            if first[c] is not None and proc.read(c, 0x2000) != first[c]:
                moving.append(c)
        except CollectionError:
            continue
    return ([c for c in aligned if c in moving]
            + [c for c in moving if c not in aligned]
            + [c for c in aligned if c not in moving])


# ---------------------------------------------------------------------------
# The RamBackend over process memory
# ---------------------------------------------------------------------------

class CollectionBackend:
    """``RamBackend`` implementation over an anchored game.exe.

    ``guarded_write`` is read-compare-write: unlike BizHawk's frame-atomic version there
    is a small race window between the compare and the write, which the callers'
    retry-next-tick contract already tolerates (see ram/backend.py)."""

    def __init__(self, proc: GameProcess, rom_base: int, ewram_base: int) -> None:
        self.proc = proc
        self.rom_base = rom_base
        self.ewram_base = ewram_base

    def _host(self, offset: int, size: int, domain: str) -> int:
        if domain == EWRAM:
            base, limit = self.ewram_base, EWRAM_SIZE
        elif domain == ROM_DOMAIN:
            base, limit = self.rom_base, ROM_SIZE
        else:
            raise CollectionError(f"unsupported memory domain {domain!r}")
        if not 0 <= offset <= limit - size:
            raise CollectionError(f"{domain} access out of range: {offset:#x}+{size:#x}")
        return base + offset

    async def read_many(self, requests: Sequence[tuple[int, int, str]]) -> list[bytes]:
        return [self.proc.read(self._host(offset, size, domain), size)
                for offset, size, domain in requests]

    async def write(self, offset: int, data: Sequence[int], domain: str) -> None:
        payload = bytes(data)
        self.proc.write(self._host(offset, len(payload), domain), payload)

    async def guarded_write(self, offset: int, data: Sequence[int],
                            expected: Sequence[int], domain: str) -> bool:
        payload, guard = bytes(data), bytes(expected)
        address = self._host(offset, len(payload), domain)
        if self.proc.read(address, len(guard)) != guard:
            return False
        self.proc.write(address, payload)
        return True


# ---------------------------------------------------------------------------
# Client context + watcher
# ---------------------------------------------------------------------------

from CommonClient import CommonContext, get_base_parser, gui_enabled, logger, server_loop  # noqa: E402


class CVAOSCollectionContext(CommonContext):
    game = "Castlevania - Aria of Sorrow"
    items_handling = 0b001
    want_slot_data = True

    def __init__(self, server_address: str | None, password: str | None) -> None:
        super().__init__(server_address, password)
        self.brain = CVAOSClientLogic()
        self.backend: CollectionBackend | None = None
        self.slot_data: dict = {}
        self.password_requested = False
        self._last_attach_attempt = 0.0
        self._last_revalidate = 0.0
        self._attach_error_shown: str | None = None

    # -- AP plumbing --------------------------------------------------------
    async def server_auth(self, password_requested: bool = False) -> None:
        self.password_requested = password_requested
        if self.backend is None:
            logger.info("Awaiting attach to the Advance Collection before authenticating.")
            return
        if self.auth is None:
            # Same contract as the BizHawk client's set_auth: the 16 auth bytes the patch
            # baked into ROM free space, registered server-side under connect_names.
            auth_raw = self.backend.proc.read(self.backend.rom_base + AUTH_NUMBER_START, 16)
            self.auth = base64.b64encode(auth_raw).decode("ascii")
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        await self.send_connect()

    def on_package(self, cmd: str, args: dict) -> None:
        if cmd == "Connected":
            self.slot_data = args.get("slot_data", {}) or {}
            target = self.slot_data.get("target_platform")
            if target is not None and int(target) != TARGET_ADVANCE_COLLECTION:
                logger.warning(
                    "This seed's target platform is GBA (BizHawk), not the Advance Collection. "
                    "It will still play here, but double-check you connected the intended client.")
        self.brain.queue_deathlink_from_bounce(self, cmd, args)

    def run_gui(self) -> None:
        from kvui import GameManager

        class CVAOSCollectionManager(GameManager):
            logging_pairs = [("Client", "Archipelago")]
            base_title = "Archipelago CVAoS Collection Client"

        self.ui = CVAOSCollectionManager(self)
        self.ui_task = asyncio.create_task(self.ui.async_run(), name="UI")

    # -- attach/anchor lifecycle ---------------------------------------------
    def _detach(self, reason: str) -> None:
        if self.backend is not None:
            logger.info("Lost the Advance Collection (%s); will re-attach.", reason)
            self.backend.proc.close()
            self.backend = None
        self.auth = None

    def try_attach(self) -> None:
        """One attach+anchor attempt, rate-limited; safe to call every tick."""
        now = time.monotonic()
        if now - self._last_attach_attempt < ATTACH_RETRY_SECONDS:
            return
        self._last_attach_attempt = now
        try:
            proc = GameProcess.attach()
            try:
                rom = find_rom_image(proc)
                ewram_base = find_ewram_base(proc, rom)
            except CollectionError:
                proc.close()
                raise
        except CollectionError as exc:
            # Log each distinct problem once, not every 5 seconds.
            if str(exc) != self._attach_error_shown:
                logger.info("%s", exc)
                self._attach_error_shown = str(exc)
            return
        self._attach_error_shown = None
        self.backend = CollectionBackend(proc, rom.base, ewram_base)
        self.brain._reset_session_state()
        self._last_revalidate = now
        logger.info("Attached to the Advance Collection (ROM at %#x, EWRAM at %#x).",
                    rom.base, ewram_base)

    def revalidate_anchor(self) -> None:
        """Cheap periodic check that the anchored ROM is still ours -- switching games in
        the collection or resetting can reallocate the emulated memory without killing
        the process."""
        now = time.monotonic()
        if self.backend is None or now - self._last_revalidate < REVALIDATE_SECONDS:
            return
        self._last_revalidate = now
        marker = self.backend.proc.read(
            self.backend.rom_base + ARCHIPELAGO_IDENTIFIER_START, len(ARCHIPELAGO_IDENTIFIER))
        if marker != ARCHIPELAGO_IDENTIFIER.encode("ascii"):
            raise CollectionError("ROM identifier no longer at its anchor (game switched?)")


async def _watcher(ctx: CVAOSCollectionContext) -> None:
    while not ctx.exit_event.is_set():
        await asyncio.sleep(TICK_SECONDS)
        try:
            if ctx.backend is None:
                ctx.try_attach()
                if ctx.backend is None:
                    continue
            ctx.revalidate_anchor()
            # Kick authentication once both sides are up (mirrors the BizHawk watcher:
            # server_auth bails while unattached, so it must be retried after attach).
            if ctx.server is not None and not ctx.server.socket.closed:
                if ctx.slot is None and ctx.auth is None:
                    Utils.async_start(ctx.server_auth(ctx.password_requested))
            if ctx.server is None or ctx.slot is None:
                continue
            await ctx.brain._tick(ctx, AoSRAM(ctx.backend))
        except CollectionError as exc:
            ctx._detach(str(exc))
        except Exception:
            # Anything else raised by a tick must not end the watcher: the AP socket would stay
            # up and the client would look connected while doing nothing. Log it and back off.
            logger.exception("CVAoS collection watcher: unexpected error; retrying shortly")
            await asyncio.sleep(ERROR_BACKOFF_SECONDS)


def launch(*launch_args: str) -> None:
    async def _main() -> None:
        parser = get_base_parser(description="Castlevania AoS client for the Steam "
                                             "Castlevania Advance Collection.")
        args = parser.parse_args(launch_args)

        ctx = CVAOSCollectionContext(args.connect, args.password)
        ctx.server_task = asyncio.create_task(server_loop(ctx), name="server loop")
        watcher_task = asyncio.create_task(_watcher(ctx), name="cvaos collection watcher")

        if gui_enabled:
            ctx.run_gui()
        ctx.run_cli()

        await ctx.exit_event.wait()
        watcher_task.cancel()
        if ctx.backend is not None:
            ctx.backend.proc.close()
        await ctx.shutdown()

    Utils.init_logging("CVAoSCollectionClient", exception_logger="Client")
    import ModuleUpdate
    ModuleUpdate.update()
    asyncio.run(_main())
