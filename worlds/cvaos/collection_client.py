"""
Steam Advance Collection client for Castlevania: Aria of Sorrow.

The Castlevania Advance Collection (Steam AppID 1552550, developer M2) runs AoS inside
M2's proprietary emulator, which has no connector of any kind. This client therefore attaches
to the ``game.exe`` process directly (pymem / ReadProcessMemory) and implements the
``RamBackend`` primitives with raw process-memory reads and writes. The game logic is the same
``client_logic.CVAOSClientLogic`` that the BizHawk client uses; only the transport differs.

Anchoring means finding the emulated GBA memory inside game.exe's address space. Both steps
below rely on layouts observed on the live process.

* ROM image: located by validating a real GBA header. The "CASTLEVANIA2" title at +0xA0 must
  be backed by the Nintendo logo at +0x04 and the "A2CE" game code at +0xAC, which tells the
  ROM apart from the many stray copies of the title string in memory. The ROM sits in its own
  committed private-RW region. When the US Aria of Sorrow is absent, the same scan recognizes
  the collection's other eight GBA ROMs by their game codes, so the error message can name the
  game or region that is loaded instead.

  We then read ARCHIPELAGO_IDENTIFIER at ROM+0x670000 to confirm that the ROM is patched by a
  compatible version. Without the identifier the ROM is unpatched, and the error points at the
  ROM installer. A different version suffix means a seed from another apworld version.
* EWRAM: M2 heap-allocates the emulated GBA memory, and no static pointer to it exists in
  game.exe. The emulator's allocator prefixes every block with a header whose ``next`` and
  ``prev`` words link the blocks. The GBA-memory object is allocated right before the ROM
  buffer, so the ROM block's ``prev`` points to the GBA-memory block, and EWRAM sits 0x170
  into it. The link is checked in both directions, and each block's recorded size must equal
  the size of its committed region. This works from any screen, menus included.

  When that layout is not found, we identify the live 256 KiB EWRAM by an in-game content
  signature. All of these must hold at the exact EWRAM offsets at once: GAME_STATE==INGAME
  and MENU_STATE==NORMAL, self-consistent vitals (1<=HP<=MaxHP<=2000, MP<=MaxMP), a sane area
  id and gold, and every consumable count 0..9. The ROM's own region is skipped, because ROM
  data can match by chance. We check region_base + 0x170 first, where the GBA-memory struct
  puts EWRAM, and fall back to a full 0x10-stride scan only if nothing matches there.

  The signature requires normal gameplay, so this fallback succeeds only while the player is
  in a room, and the client keeps retrying until then. The signature can also match more than
  one block. The collection's rewind buffer can hold flat snapshots, and on the live process,
  frozen look-alike buffers (HP 3/3, every count 3) pass the signature while the player sits
  in a menu. Snapshots and look-alikes are frozen, so a match is accepted only if it is
  struct-aligned or its low memory changes between two reads.

Failure handling: every pymem or OS error is wrapped in ``CollectionError``. The watcher then
drops to "detached" and keeps the AP connection up, and it tries to re-attach and re-anchor
every 5 s. While attached, it also revalidates the ROM anchor every 2 s, because switching games
inside the collection or resetting can silently reallocate the emulated memory.
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import time
from typing import TYPE_CHECKING, NamedTuple, Sequence

import Utils

from ._bytemaker_compat import Entry
from .client_logic import CVAOSClientLogic, items_handling_for
from .options import TARGET_ADVANCE_COLLECTION
from .ram import AoSRAM, addresses as addr
from .ram.addresses import EWRAM
from .ram.backend import GuardedWrite
from .rom import (ARCHIPELAGO_IDENTIFIER, ARCHIPELAGO_IDENTIFIER_START, AUTH_NUMBER_START,
                  COMPATIBLE_IDENTIFIERS)

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
# we read the marker there to confirm that the ROM is patched, and by which version.
_IDENTIFIER_BYTES = ARCHIPELAGO_IDENTIFIER.encode("ascii")
_IDENTIFIER_PREFIX = _IDENTIFIER_BYTES[:9]  # b"CVAOS_AP_", shared across patch versions
_COMPATIBLE_BYTES = frozenset(ident.encode("ascii") for ident in COMPATIBLE_IDENTIFIERS)

# GBA ROM header validation: the fixed Nintendo logo starts at ROM+0x04, and Aria of Sorrow
# (USA) has game code "A2CE" at 0xAC. Title + logo + code uniquely identify the true ROM
# image among the many stray copies of the "CASTLEVANIA2" string in process memory.
_ROM_LOGO_OFFSET = 0x04
_ROM_LOGO_PREFIX = bytes.fromhex("24ffae5169")  # first 5 bytes of the GBA logo
_ROM_GAMECODE_OFFSET = 0xAC
_ROM_GAMECODE = b"A2CE"


class CollectionRom(NamedTuple):
    title: bytes   # the header's internal title at +0xA0
    game: str
    region: str

    @property
    def label(self) -> str:
        return f"{self.game} ({self.region})"


# The collection's nine GBA ROMs, keyed by the game code in each ROM's header. The codes come from
# the ROMs in the collection's archive. The installer replaces only the US Aria of Sorrow, so every
# other entry here is a game or region the player has to switch away from.
COLLECTION_GBA_ROMS: dict[bytes, CollectionRom] = {
    b"AAME": CollectionRom(b"DRACULA AGB1", "Circle of the Moon", "US"),
    b"AAMJ": CollectionRom(b"DRACULA AGB1", "Circle of the Moon", "Japan"),
    b"AAMP": CollectionRom(b"DRACULA AGB1", "Circle of the Moon", "Europe"),
    b"ACHE": CollectionRom(b"CASTLEVANIA1", "Harmony of Dissonance", "US"),
    b"ACHJ": CollectionRom(b"CASTLEVANIA1", "Harmony of Dissonance", "Japan"),
    b"ACHP": CollectionRom(b"CASTLEVANIA1", "Harmony of Dissonance", "Europe"),
    b"A2CE": CollectionRom(ROM_TITLE, "Aria of Sorrow", "US"),
    b"A2CJ": CollectionRom(ROM_TITLE, "Aria of Sorrow", "Japan"),
    b"A2CP": CollectionRom(ROM_TITLE, "Aria of Sorrow", "Europe"),
}

# In-game EWRAM signature bounds. Loose enough never to reject a real save, tight enough to
# exclude essentially all random memory (verified against the live process).
_MAX_VITAL = 2000      # AoS HP/MP never approach this
_MAX_GOLD = 999_999   # the game's own gold cap (ram/addresses.py GOLD_CAP)
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

# The emulator's allocator header, 0x20 bytes before every block's payload: next, prev, two
# zero words, the block size (equal to the committed region's size), a second size, a cookie,
# and flags. The ROM image starts right after its header; the GBA-memory object's EWRAM
# member is _EWRAM_STRUCT_OFFSET into its block (header included).
_ALLOC_HEADER = 0x20
_ALLOC_NEXT, _ALLOC_PREV, _ALLOC_SIZE = 0x00, 0x04, 0x10

ATTACH_RETRY_SECONDS = 5.0
REVALIDATE_SECONDS = 2.0
TICK_SECONDS = 0.125
ERROR_BACKOFF_SECONDS = 5.0  # pause after an unexpected tick error before ticking again


class CollectionError(Exception):
    """Failure to attach to game.exe, to anchor the game's memory in it, or to access that memory.

    The causes include a collection that is not running, a wrong or unpatched ROM, a game that is
    not in normal gameplay yet, and a failed read or write. The client logs the message for the
    player and tries again later."""


# ---------------------------------------------------------------------------
# Win32 region enumeration (VirtualQueryEx), the one piece pymem doesn't wrap nicely.
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
        # MEM_PRIVATE committed RW, which is where a heap-allocated EWRAM buffer must live.
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
    """Yield every memory region of the target process, whatever its state.

    The walk stops at 4 GiB, because game.exe is a 32-bit process."""
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
    """An attached game.exe process.

    It reads and writes raw memory and lists the process's memory regions. Attach, read, and
    write failures are raised as ``CollectionError``."""

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
                "The Castlevania Advance Collection is not running. Launch it from Steam and "
                "start the US version of Aria of Sorrow.") from exc
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


def collection_roms_in(buf: bytes, region_base: int) -> list[tuple[int, bytes]]:
    """Return the host address and game code of every collection ROM header in ``buf``.

    ``region_base`` is the host address of ``buf[0]``. A header counts only when the start of
    the Nintendo logo, a game code from :data:`COLLECTION_GBA_ROMS`, and that code's title all
    sit at their header offsets.
    """
    found = []
    idx = buf.find(_ROM_LOGO_PREFIX)
    while idx != -1:
        rom_off = idx - _ROM_LOGO_OFFSET
        code = bytes(buf[rom_off + _ROM_GAMECODE_OFFSET:rom_off + _ROM_GAMECODE_OFFSET + 4])
        rom = COLLECTION_GBA_ROMS.get(code)
        if (rom_off >= 0 and rom is not None
                and buf[rom_off + ROM_TITLE_OFFSET:rom_off + ROM_TITLE_OFFSET + len(rom.title)]
                == rom.title):
            found.append((region_base + rom_off, code))
        idx = buf.find(_ROM_LOGO_PREFIX, idx + 1)
    return found


def _wrong_rom_message(codes: set[bytes]) -> str:
    """Return the error for a collection that has loaded anything but US Aria of Sorrow, or
    nothing at all.

    If another region of Aria of Sorrow is among ``codes``, the message names only that one,
    because it is the likelier mistake.
    """
    roms = [COLLECTION_GBA_ROMS[code] for code in sorted(codes)]
    other_regions = [rom for rom in roms if rom.title == ROM_TITLE]
    if other_regions:
        return (f"The collection has loaded {other_regions[0].label}, but Archipelago only "
                f"patches the US version. Return to the collection's menu and start the US "
                f"version instead.")
    if roms:
        loaded = " and ".join(rom.label for rom in roms)
        return (f"The collection has loaded {loaded}, not Aria of Sorrow. Return to the "
                f"collection's menu and start the US version of Aria of Sorrow.")
    return ("Aria of Sorrow is not loaded in the collection yet. Start the US version of Aria "
            "of Sorrow from the collection's menu, and the client will detect it once the game "
            "is running.")


def find_rom_image(proc: GameProcess) -> RomImage:
    """Locate the loaded AoS ROM by validating its GBA header, then confirm it is patched
    with a compatible AP identifier.

    Raise CollectionError with an actionable message otherwise. If the collection has loaded
    another game or region, the message names it.
    """
    others: set[bytes] = set()
    for region in proc.regions():
        if not region.readable or region.size < 0x10000:
            continue
        try:
            buf = proc.read(region.base, region.size)
        except CollectionError:
            continue  # region vanished mid-scan; harmless
        roms = collection_roms_in(buf, region.base)
        rom_base = next((base for base, code in roms if code == _ROM_GAMECODE), None)
        if rom_base is None:
            # The emulator puts the ROM it runs right after its block's allocation header (see
            # ewram_via_allocation_links). A header anywhere else may be a stale copy, so it
            # is not named in the error.
            others.update(code for base, code in roms if base == region.base + _ALLOC_HEADER)
            continue
        # This region holds the US Aria of Sorrow. The client can only play a patched ROM,
        # because it reads the identifier and auth from the ROM, and its game logic relies on
        # the patch's hooks.
        marker = proc.read(rom_base + ARCHIPELAGO_IDENTIFIER_START, len(_IDENTIFIER_BYTES))
        if marker in _COMPATIBLE_BYTES:
            return RomImage(rom_base, region.base, region.base + region.size)
        if marker.startswith(_IDENTIFIER_PREFIX):
            ver = marker.split(b"\x00", 1)[0].decode("ascii", "replace")
            raise CollectionError(
                f"The installed seed uses patch format {ver}, but this client reads "
                f"{ARCHIPELAGO_IDENTIFIER}. Install a seed generated with this apworld, or "
                f"switch to the apworld that generated the seed.")
        raise CollectionError(
            "Aria of Sorrow (US) is running, but its ROM is not patched for Archipelago. Close "
            "the collection, install your .apcvaos with \"CVAoS Collection ROM Installer\" in "
            "the Archipelago Launcher, then start Aria of Sorrow (US) again.")
    raise CollectionError(_wrong_rom_message(others))


def ewram_signature_ok(buf: bytes, off: int) -> bool:
    """Return whether ``buf`` at ``off`` looks like live AoS EWRAM during normal gameplay.

    The game and menu states must show the player in a room. HP and MP must be within their
    maximums, the area id and gold must be in range, and every consumable count must be at
    most 9. EWRAM discovery falls back to this check when the allocation links fail (see the
    module docstring)."""
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
    """Yield the private read-write regions big enough to hold EWRAM.

    Regions that overlap the ROM's own region are skipped, because ROM data can satisfy the
    signature by chance."""
    for region in proc.regions():
        if not region.writable_private or region.size < EWRAM_SIZE:
            continue
        if region.base < rom.region_end and region.base + region.size > rom.region_start:
            continue
        yield region


def _u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "little")


def ewram_via_allocation_links(proc: GameProcess, rom: RomImage) -> int | None:
    """Return the EWRAM base found through the emulator's allocation list, or None.

    The ROM's block must start its region and record that region's size. Its ``prev`` link
    must point to the start of a private read-write region whose block links back to the ROM
    block and records its own region's size. EWRAM is then ``_EWRAM_STRUCT_OFFSET`` bytes into
    that block. None means the layout differs or a read failed.

    This works whatever the game is showing.
    """
    block = rom.base - _ALLOC_HEADER
    if block != rom.region_start:
        return None
    try:
        header = proc.read(block, _ALLOC_HEADER)
        prev = _u32(header, _ALLOC_PREV)
        if prev == 0 or _u32(header, _ALLOC_SIZE) != rom.region_end - rom.region_start:
            return None
        region = next((r for r in proc.regions() if r.base == prev), None)
        if (region is None or not region.writable_private
                or region.size < _EWRAM_STRUCT_OFFSET + EWRAM_SIZE):
            return None
        candidate = proc.read(prev, _ALLOC_HEADER)
        if _u32(candidate, _ALLOC_NEXT) != block or _u32(candidate, _ALLOC_SIZE) != region.size:
            return None
    except CollectionError:
        return None
    return prev + _EWRAM_STRUCT_OFFSET


def find_ewram_base(proc: GameProcess, rom: RomImage) -> int:
    """Return the host address of the running game's EWRAM.

    The allocation links are tried first, and they work from any screen. When they fail, the
    content signature is checked at the emulator's struct offset (0x170) in each candidate
    region outside the ROM's. A full 0x10-stride scan of those regions runs only if nothing
    matches there.

    Rewind snapshots and look-alike buffers can also match, so :func:`_live_candidates` picks
    among the matches by alignment and by whether their memory changes. Raise CollectionError
    when no match is left."""
    linked = ewram_via_allocation_links(proc, rom)
    if linked is not None:
        return linked

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
        # Nothing matched at the struct offset (an unexpected build or state), so scan every
        # region in 0x10-byte steps.
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
    """Return the candidates that can be the running game's EWRAM, most likely first.

    A lone struct-aligned match is returned as is. Otherwise each candidate's low memory is
    read twice, a few frames apart. The live buffer changes every frame during play, while
    rewind snapshots and look-alike buffers are frozen.

    Aligned candidates that changed come first, then unaligned ones that changed, then aligned
    ones that did not (a paused game). Unaligned candidates that did not change are dropped,
    because on the real process such blocks pass the content signature without being the
    game's memory.
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

    ``guarded_write`` and ``guarded_write_many`` read, compare, then write. Unlike BizHawk's
    frame-atomic versions, they leave a small race window between the compare and the write.
    Callers retry a failed guard on the next tick, and that retry contract (see ram/backend.py)
    tolerates the window."""

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

    async def guarded_write_many(self, writes: Sequence[GuardedWrite]) -> bool:
        # Resolve every address and check every guard before the first write, so an out-of-range
        # entry or a stale guard leaves memory untouched.
        targets = [(self._host(w.offset, len(w.data), w.domain), bytes(w.data), bytes(w.expected))
                   for w in writes]
        if any(self.proc.read(address, len(guard)) != guard for address, _, guard in targets):
            return False
        for address, payload, _ in targets:
            self.proc.write(address, payload)
        return True


# ---------------------------------------------------------------------------
# Client context + watcher
# ---------------------------------------------------------------------------

from CommonClient import (ClientCommandProcessor, CommonContext, get_base_parser,  # noqa: E402
                          gui_enabled, logger, server_loop)


class CVAOSCollectionCommandProcessor(ClientCommandProcessor):
    def _cmd_map(self) -> None:
        """Open the map tracker for this slot in your browser."""
        from .tracker_app import open_for_client
        open_for_client(self.ctx)


class CVAOSCollectionContext(CommonContext):
    game = "Castlevania - Aria of Sorrow"
    # Items from other worlds plus the starting inventory, as in the BizHawk Client.
    items_handling = 0b101
    want_slot_data = True
    command_processor = CVAOSCollectionCommandProcessor

    def __init__(self, server_address: str | None, password: str | None) -> None:
        super().__init__(server_address, password)
        self.brain = CVAOSClientLogic()
        self.backend: CollectionBackend | None = None
        self.slot_data: dict = {}
        self.password_requested = False
        self._last_attach_attempt = 0.0
        self._last_revalidate = 0.0
        self._attach_error_shown: str | None = None
        # The auth of the seed the current connection plays, to notice a different seed installed
        # while the game was closed.
        self._connected_auth: str | None = None
        # True from noticing a different installed seed until the reconnect for it completes. The
        # watcher does not tick in between, because the connection still belongs to the old seed.
        self._seed_changed = False

    # -- AP plumbing --------------------------------------------------------
    async def server_auth(self, password_requested: bool = False) -> None:
        self.password_requested = password_requested
        if self.backend is None:
            logger.info("Awaiting attach to the Advance Collection before authenticating.")
            return
        if self.auth is None:
            # As in the BizHawk client's set_auth, the auth is the 16 bytes the patch writes into
            # ROM free space. The world registers the same value under connect_names.
            auth_raw = self.backend.proc.read(self.backend.rom_base + AUTH_NUMBER_START, 16)
            self.auth = base64.b64encode(auth_raw).decode("ascii")
        # A seed with enemy checks also asks for this player's own items, as the BizHawk Client does.
        self.items_handling = items_handling_for(self.backend.proc.read(
            self.backend.rom_base + ARCHIPELAGO_IDENTIFIER_START, len(ARCHIPELAGO_IDENTIFIER)))
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        await self.send_connect()

    def on_package(self, cmd: str, args: dict) -> None:
        if cmd == "Connected":
            logger.info('Press "Open map" or type /map to open the map tracker for this slot in '
                        "your browser.")
            # A reconnect to the same seed starts a new server session, so the checks sent in the
            # last session are sent again (the server ignores repeats). Otherwise a check sent into
            # a connection that had already died would never arrive.
            self.brain._reset_session_state(same_seed=self.auth == self._connected_auth)
            self._connected_auth = self.auth
            self._seed_changed = False
            self.slot_data = args.get("slot_data", {}) or {}
            target = self.slot_data.get("target_platform")
            if target is not None and int(target) != TARGET_ADVANCE_COLLECTION:
                logger.warning(
                    "This seed's target platform is GBA (BizHawk). You can play it in the Advance "
                    "Collection, but the intended client is the BizHawk Client.")
        self.brain.queue_deathlink_from_bounce(self, cmd, args)

    def run_gui(self) -> None:
        from kvui import GameManager

        class CVAOSCollectionManager(GameManager):
            logging_pairs = [("Client", "Archipelago")]
            base_title = "Archipelago CVAoS Collection Client"

            def build(self):
                layout = super().build()
                from .tracker_app import add_map_button
                add_map_button(self.ctx)
                return layout

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
        """Try to attach to game.exe and anchor its memory, at most once per retry interval.

        A call inside the interval returns at once, so this is safe to call every tick."""
        now = time.monotonic()
        if now - self._last_attach_attempt < ATTACH_RETRY_SECONDS:
            return
        self._last_attach_attempt = now
        try:
            proc = GameProcess.attach()
            try:
                rom = find_rom_image(proc)
                ewram_base = find_ewram_base(proc, rom)
                installed = base64.b64encode(
                    proc.read(rom.base + AUTH_NUMBER_START, 16)).decode("ascii")
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
        self.brain._reset_session_state(same_seed=installed == self._connected_auth)
        self._last_revalidate = now
        logger.info("Attached to the Advance Collection (ROM at %#x, EWRAM at %#x).",
                    rom.base, ewram_base)
        if self._connected_auth not in (None, installed):
            # The installed seed is not the one this client last played. The old seed's goal must
            # not carry over, because CommonClient reports CLIENT_GOAL on every Connected while
            # finished_game is set.
            self.finished_game = False
            self._connected_auth = None
            if self.slot is not None:
                # The open connection still plays the old seed's slot. Reconnecting authenticates
                # with the new seed's auth.
                logger.info("The collection now has a different seed installed, so the client is "
                            "reconnecting as that seed's slot.")
                self._seed_changed = True
                Utils.async_start(self.disconnect(allow_autoreconnect=True))

    def revalidate_anchor(self) -> None:
        """Check that the anchored ROM still carries a compatible identifier.

        The check runs at most once per revalidation interval. It is needed because switching
        games in the collection, or resetting, can reallocate the emulated memory while the
        process keeps running. Raise CollectionError when the identifier is gone."""
        now = time.monotonic()
        if self.backend is None or now - self._last_revalidate < REVALIDATE_SECONDS:
            return
        self._last_revalidate = now
        marker = self.backend.proc.read(
            self.backend.rom_base + ARCHIPELAGO_IDENTIFIER_START, len(ARCHIPELAGO_IDENTIFIER))
        if marker not in _COMPATIBLE_BYTES:
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
            # Start authentication once both the game and the server are connected, as the
            # BizHawk watcher does. server_auth returns early while the game is not attached, so
            # it has to be retried after attach.
            if ctx.server is not None and not ctx.server.socket.closed:
                if ctx.slot is None and ctx.auth is None:
                    Utils.async_start(ctx.server_auth(ctx.password_requested))
            if ctx.server is None or ctx.slot is None or ctx._seed_changed:
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
