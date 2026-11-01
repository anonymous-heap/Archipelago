"""
Offline tests for the Advance Collection process-attach backend (no game.exe needed).

These pin the two things that must be exactly right for the collection client to be safe:
the GBA->host address translation + guarded-write compare-and-swap in ``CollectionBackend``,
and the anchor-discovery logic that decides WHERE in the process to point it
(``validated_rom_base_in`` / ``ewram_signature_ok``). A ``FakeProc`` stands in for a
live process's sparse memory so we never touch pymem.

    python -m pytest worlds/cvaos/test/test_collection_backend.py -v
"""
from __future__ import annotations

import asyncio
import base64
import unittest

from ..collection_client import (
    COLLECTION_GBA_ROMS, EWRAM_SIZE, ROM_SIZE, ROM_TITLE, CollectionBackend, CollectionError,
    _EWRAM_STRUCT_OFFSET, _ROM_GAMECODE, _ROM_GAMECODE_OFFSET, _ROM_LOGO_OFFSET,
    _ROM_LOGO_PREFIX, RomImage, collection_roms_in, ewram_offset, ewram_signature_ok,
    find_ewram_base, find_rom_image, validated_rom_base_in,
)
from ..ram import addresses as addr
from ..ram.addresses import EWRAM, GameState
from ..ram.backend import GuardedWrite
from ..rom import ARCHIPELAGO_IDENTIFIER, ARCHIPELAGO_IDENTIFIER_START

ROM_DOMAIN = "ROM"
ROM_TITLE_OFFSET = 0xA0

# EWRAM-domain offsets of the signature fields (the map declares them by GBA address).
GAME_STATE, MENU_STATE = ewram_offset(addr.GAME_STATE), ewram_offset(addr.MENU_STATE)
CURRENT_HP, MAX_HP = ewram_offset(addr.CURRENT_HP), ewram_offset(addr.MAX_HP)
CURRENT_MP, MAX_MP = ewram_offset(addr.CURRENT_MP), ewram_offset(addr.MAX_MP)
CURRENT_AREA, CURRENT_GOLD = ewram_offset(addr.CURRENT_AREA), ewram_offset(addr.CURRENT_GOLD)
CONSUMABLE_BASE = ewram_offset(addr.INVENTORY["consumable"].entry)


def _run(coro):
    return asyncio.run(coro)


def _make_ewram_ingame(overrides: dict[int, int] | None = None) -> bytearray:
    """A buffer whose bytes at offset 0 satisfy the in-game EWRAM signature, before overrides."""
    buf = bytearray(EWRAM_SIZE)
    buf[GAME_STATE] = int(GameState.INGAME)     # 0x04
    buf[MENU_STATE] = addr.MENU_STATE_NORMAL     # 0x01
    buf[CURRENT_AREA] = 0x04
    buf[CURRENT_HP:CURRENT_HP + 2] = (596).to_bytes(2, "little")
    buf[MAX_HP:MAX_HP + 2] = (596).to_bytes(2, "little")
    buf[CURRENT_MP:CURRENT_MP + 2] = (235).to_bytes(2, "little")
    buf[MAX_MP:MAX_MP + 2] = (235).to_bytes(2, "little")
    buf[CURRENT_GOLD:CURRENT_GOLD + 4] = (3175).to_bytes(4, "little")
    for off, val in (overrides or {}).items():
        buf[off] = val
    return buf


def _make_rom_header(code: bytes = _ROM_GAMECODE) -> bytes:
    """Return minimal bytes holding a valid collection ROM header at offset 0.

    The header has the title, the Nintendo logo and the game code of ``code``, which defaults to
    the US Aria of Sorrow.
    """
    title = COLLECTION_GBA_ROMS[code].title
    buf = bytearray(0x200)
    buf[ROM_TITLE_OFFSET:ROM_TITLE_OFFSET + len(title)] = title
    buf[_ROM_LOGO_OFFSET:_ROM_LOGO_OFFSET + len(_ROM_LOGO_PREFIX)] = _ROM_LOGO_PREFIX
    buf[_ROM_GAMECODE_OFFSET:_ROM_GAMECODE_OFFSET + len(code)] = code
    return bytes(buf)


class FakeProc:
    """Sparse process memory: any unset address reads as 0, like committed-but-zeroed pages."""

    def __init__(self) -> None:
        self.mem: dict[int, int] = {}

    def read(self, address: int, size: int) -> bytes:
        return bytes(self.mem.get(address + i, 0) for i in range(size))

    def write(self, address: int, data) -> None:
        for i, b in enumerate(bytes(data)):
            self.mem[address + i] = b


class _ScanProc(FakeProc):
    """FakeProc with region enumeration and, for chosen bases, low memory that changes on
    every read (a running game's frame counters)."""

    def __init__(self, regions, moving=()) -> None:
        super().__init__()
        self._regions = list(regions)
        self.moving = set(moving)
        self.reads = 0

    def regions(self):
        return list(self._regions)

    def read(self, address: int, size: int) -> bytes:
        data = bytearray(super().read(address, size))
        if address in self.moving:
            self.reads += 1
            data[0] = self.reads & 0xFF
        return bytes(data)


class EwramDiscoveryTest(unittest.TestCase):
    """find_ewram_base must not anchor on a frozen buffer that merely passes the signature."""

    REGION = 0x10000000
    ROM = RomImage(0x30000000, 0x30000000, 0x30800000)

    def _proc(self, offsets, moving=()):
        from ..collection_client import _Region
        region = _Region(self.REGION, EWRAM_SIZE * 2, protect=0x04, state=0x1000, mem_type=0x20000)
        proc = _ScanProc([region], moving={self.REGION + off for off in moving})
        for off in offsets:
            proc.write(self.REGION + off, bytes(_make_ewram_ingame()[:0x14000]))
        return proc

    def _linked_proc(self, back_link_ok: bool = True):
        from ..collection_client import _Region
        rom_block, ewram_block, rom_size = 0x30000000, self.REGION, 0x801000
        # The image starts right after the block's 0x20-byte header, as on the real process.
        rom = RomImage(rom_block + 0x20, rom_block, rom_block + rom_size)
        ewram_size = EWRAM_SIZE + 0x1000
        proc = _ScanProc([_Region(ewram_block, ewram_size, 0x04, 0x1000, 0x20000),
                          _Region(rom_block, rom_size, 0x04, 0x1000, 0x20000)])
        proc.write(rom_block, (0x31000000).to_bytes(4, "little") + ewram_block.to_bytes(4, "little")
                   + bytes(8) + rom_size.to_bytes(4, "little"))
        back = rom_block if back_link_ok else 0x0BAD0000
        proc.write(ewram_block, back.to_bytes(4, "little") + (0x0F000000).to_bytes(4, "little")
                   + bytes(8) + ewram_size.to_bytes(4, "little"))
        return proc, rom

    def test_allocation_links_settle_ewram_without_gameplay(self) -> None:
        # No content signature anywhere (the player is in a menu); the links alone suffice.
        proc, rom = self._linked_proc()
        self.assertEqual(find_ewram_base(proc, rom), self.REGION + _EWRAM_STRUCT_OFFSET)

    def test_broken_back_link_falls_back_to_the_signature(self) -> None:
        proc, rom = self._linked_proc(back_link_ok=False)
        with self.assertRaisesRegex(CollectionError, "not in normal gameplay"):
            find_ewram_base(proc, rom)

    def test_lone_struct_aligned_match_is_taken(self) -> None:
        proc = self._proc([_EWRAM_STRUCT_OFFSET])
        self.assertEqual(find_ewram_base(proc, self.ROM), self.REGION + _EWRAM_STRUCT_OFFSET)

    def test_frozen_unaligned_look_alike_is_rejected(self) -> None:
        proc = self._proc([0x40080])
        with self.assertRaisesRegex(CollectionError, "frozen look-alike"):
            find_ewram_base(proc, self.ROM)

    def test_unaligned_but_moving_buffer_is_accepted(self) -> None:
        proc = self._proc([0x40080], moving=[0x40080])
        self.assertEqual(find_ewram_base(proc, self.ROM), self.REGION + 0x40080)

    def test_aligned_beats_a_frozen_look_alike(self) -> None:
        proc = self._proc([_EWRAM_STRUCT_OFFSET, 0x40080])
        self.assertEqual(find_ewram_base(proc, self.ROM), self.REGION + _EWRAM_STRUCT_OFFSET)

    def test_no_match_at_all_asks_for_gameplay(self) -> None:
        with self.assertRaisesRegex(CollectionError, "not in normal gameplay"):
            find_ewram_base(self._proc([]), self.ROM)


class AddressTranslationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.proc = FakeProc()
        self.backend = CollectionBackend(self.proc, rom_base=0x30000000, ewram_base=0x10000000)

    def test_ewram_and_rom_domains_translate(self) -> None:
        self.proc.write(0x10000000 + 0x1327A, b"\x63\x00")  # EWRAM 0x0201327A
        self.proc.write(0x30000000 + 0xA0, ROM_TITLE)        # ROM 0xA0
        (hp,) = _run(self.backend.read_many([(0x1327A, 2, EWRAM)]))
        self.assertEqual(hp, b"\x63\x00")
        (title,) = _run(self.backend.read_many([(0xA0, len(ROM_TITLE), ROM_DOMAIN)]))
        self.assertEqual(title, ROM_TITLE)

    def test_unknown_domain_raises(self) -> None:
        with self.assertRaises(CollectionError):
            _run(self.backend.read_many([(0, 1, "IWRAM")]))

    def test_out_of_range_raises(self) -> None:
        with self.assertRaises(CollectionError):
            _run(self.backend.read_many([(EWRAM_SIZE, 1, EWRAM)]))
        with self.assertRaises(CollectionError):
            _run(self.backend.read_many([(ROM_SIZE - 1, 2, ROM_DOMAIN)]))

    def test_write_lands_at_translated_host_address(self) -> None:
        _run(self.backend.write(0x13290, [0x10, 0x20, 0x30, 0x40], EWRAM))
        self.assertEqual(self.proc.read(0x10000000 + 0x13290, 4), b"\x10\x20\x30\x40")


class GuardedWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.proc = FakeProc()
        self.backend = CollectionBackend(self.proc, rom_base=0x30000000, ewram_base=0x10000000)
        self.host = 0x10000000 + 0x1328A
        self.proc.write(self.host, b"\x05\x00")  # current AP_RECEIVED_COUNT = 5

    def test_matching_guard_writes_and_returns_true(self) -> None:
        ok = _run(self.backend.guarded_write(0x1328A, [0x06, 0x00], [0x05, 0x00], EWRAM))
        self.assertTrue(ok)
        self.assertEqual(self.proc.read(self.host, 2), b"\x06\x00")

    def test_mismatched_guard_leaves_memory_and_returns_false(self) -> None:
        ok = _run(self.backend.guarded_write(0x1328A, [0x06, 0x00], [0x04, 0x00], EWRAM))
        self.assertFalse(ok)
        self.assertEqual(self.proc.read(self.host, 2), b"\x05\x00", "must not write on a lost guard")

    def batch(self, counter_guard: bytes) -> list[GuardedWrite]:
        # Gold 0 -> 500, and the received-counter from counter_guard to 6.
        return [GuardedWrite(0x13290, (500).to_bytes(4, "little"), bytes(4), EWRAM),
                GuardedWrite(0x1328A, b"\x06\x00", counter_guard, EWRAM)]

    def test_a_batch_with_matching_guards_makes_every_write(self) -> None:
        self.assertTrue(_run(self.backend.guarded_write_many(self.batch(b"\x05\x00"))))
        self.assertEqual(self.proc.read(0x10000000 + 0x13290, 4), (500).to_bytes(4, "little"))
        self.assertEqual(self.proc.read(self.host, 2), b"\x06\x00")

    def test_one_stale_guard_leaves_the_whole_batch_unwritten(self) -> None:
        self.assertFalse(_run(self.backend.guarded_write_many(self.batch(b"\x04\x00"))))
        self.assertEqual(self.proc.read(0x10000000 + 0x13290, 4), bytes(4),
                         "the write before the stale guard must not land either")
        self.assertEqual(self.proc.read(self.host, 2), b"\x05\x00")

    def test_an_out_of_range_write_leaves_the_whole_batch_unwritten(self) -> None:
        bad = GuardedWrite(0x40000, b"\x01", b"\x00", EWRAM)
        with self.assertRaises(CollectionError):
            _run(self.backend.guarded_write_many([*self.batch(b"\x05\x00"), bad]))
        self.assertEqual(self.proc.read(0x10000000 + 0x13290, 4), bytes(4))
        self.assertEqual(self.proc.read(self.host, 2), b"\x05\x00")


class ErrorWrappingTest(unittest.TestCase):
    def test_read_failures_surface_as_collection_error(self) -> None:
        class ExplodingProc(FakeProc):
            def read(self, address, size):
                raise CollectionError("simulated read failure")

        backend = CollectionBackend(ExplodingProc(), rom_base=0, ewram_base=0)
        with self.assertRaises(CollectionError):
            _run(backend.read_many([(0x10, 1, EWRAM)]))


class RomHeaderValidationTest(unittest.TestCase):
    def test_finds_valid_header_and_returns_host_base(self) -> None:
        region_base = 0x20000000
        pad = 0x1234
        buf = bytes(pad) + _make_rom_header()
        self.assertEqual(validated_rom_base_in(buf, region_base), region_base + pad)

    def test_rejects_title_without_logo(self) -> None:
        # A stray copy of the title string (e.g. a save header) with no Nintendo logo/game code.
        buf = bytearray(0x200)
        buf[ROM_TITLE_OFFSET:ROM_TITLE_OFFSET + len(ROM_TITLE)] = ROM_TITLE
        self.assertIsNone(validated_rom_base_in(bytes(buf), 0))

    def test_rejects_wrong_game_code(self) -> None:
        header = bytearray(_make_rom_header())
        header[_ROM_GAMECODE_OFFSET:_ROM_GAMECODE_OFFSET + 4] = b"ACME"  # no collection ROM's code
        self.assertIsNone(validated_rom_base_in(bytes(header), 0))

    def test_no_title_at_all(self) -> None:
        self.assertIsNone(validated_rom_base_in(bytes(0x200), 0))

    def test_other_collection_roms_are_recognized_but_not_validated(self) -> None:
        buf = bytes(0x40) + _make_rom_header(b"A2CJ") + _make_rom_header(b"AAME")
        self.assertEqual(collection_roms_in(buf, 0x1000),
                         [(0x1040, b"A2CJ"), (0x1240, b"AAME")])
        self.assertIsNone(validated_rom_base_in(buf, 0x1000))

    def test_a_code_with_the_wrong_title_is_not_a_collection_rom(self) -> None:
        header = bytearray(_make_rom_header(b"AAME"))
        header[ROM_TITLE_OFFSET:ROM_TITLE_OFFSET + len(ROM_TITLE)] = ROM_TITLE
        self.assertEqual(collection_roms_in(bytes(header), 0), [])

    def test_a_header_that_would_start_before_the_buffer_is_ignored(self) -> None:
        # A logo at offset 0 puts the header's start 4 bytes before the buffer, while its code
        # and title would still land inside it.
        buf = bytearray(0x200)
        buf[0:len(_ROM_LOGO_PREFIX)] = _ROM_LOGO_PREFIX
        buf[_ROM_GAMECODE_OFFSET - 4:_ROM_GAMECODE_OFFSET] = _ROM_GAMECODE
        buf[ROM_TITLE_OFFSET - 4:ROM_TITLE_OFFSET - 4 + len(ROM_TITLE)] = ROM_TITLE
        self.assertEqual(collection_roms_in(bytes(buf), 0x1000), [])


class _UnreadableFirstRegion(_ScanProc):
    """A process whose first region disappears before it can be read."""

    def read(self, address: int, size: int) -> bytes:
        if address == self._regions[0].base and size == self._regions[0].size:
            raise CollectionError("simulated vanished region")
        return super().read(address, size)


class FindRomImageTest(unittest.TestCase):
    """find_rom_image anchors a patched US Aria of Sorrow, and otherwise raises an error that says
    what to fix."""

    REGION_SIZE = 0x10000
    REGION_STRIDE = 0x1000000

    def _proc(self, *codes: bytes, marker: bytes = b"", header_offset: int = 0x20,
              proc_class: type = _ScanProc) -> _ScanProc:
        """Return a process with one region per code, each holding that code's ROM header.

        The header sits ``header_offset`` bytes into its region. The default, 0x20, is where the
        emulator puts the ROM it runs. ``marker`` is written at the AP identifier's address in
        every ROM. That address lies past the end of the small fake region on purpose, because
        find_rom_image reads the identifier separately from the region scan.
        """
        from ..collection_client import _Region
        regions = [_Region(self._region(i), self.REGION_SIZE, protect=0x04, state=0x1000,
                           mem_type=0x20000) for i in range(len(codes))]
        proc = proc_class(regions)
        for i, code in enumerate(codes):
            rom_base = self._region(i) + header_offset
            proc.write(rom_base, _make_rom_header(code))
            proc.write(rom_base + ARCHIPELAGO_IDENTIFIER_START, marker)
        return proc

    def _region(self, index: int) -> int:
        return 0x20000000 + self.REGION_STRIDE * index

    def _error(self, proc: _ScanProc) -> str:
        with self.assertRaises(CollectionError) as caught:
            find_rom_image(proc)
        return str(caught.exception)

    def test_patched_us_rom_is_anchored(self) -> None:
        rom = find_rom_image(self._proc(b"A2CE", marker=ARCHIPELAGO_IDENTIFIER.encode()))
        base = self._region(0)
        self.assertEqual(rom, RomImage(base + 0x20, base, base + self.REGION_SIZE))

    def test_patched_us_rom_after_another_game_is_anchored(self) -> None:
        rom = find_rom_image(self._proc(b"AAME", b"A2CE", marker=ARCHIPELAGO_IDENTIFIER.encode()))
        self.assertEqual(rom.base, self._region(1) + 0x20)

    def test_an_unreadable_region_is_skipped(self) -> None:
        proc = self._proc(b"AAME", b"A2CE", marker=ARCHIPELAGO_IDENTIFIER.encode(),
                          proc_class=_UnreadableFirstRegion)
        self.assertEqual(find_rom_image(proc).base, self._region(1) + 0x20)

    def test_another_region_of_aria_is_named(self) -> None:
        self.assertIn("has loaded Aria of Sorrow (Japan), but Archipelago only patches the US "
                      "version", self._error(self._proc(b"A2CJ")))

    def test_another_region_of_aria_is_named_instead_of_another_game(self) -> None:
        message = self._error(self._proc(b"AAME", b"A2CP"))
        self.assertIn("has loaded Aria of Sorrow (Europe), but Archipelago only patches", message)
        self.assertNotIn("Circle of the Moon", message)

    def test_another_collection_game_is_named(self) -> None:
        self.assertIn("has loaded Harmony of Dissonance (US), not Aria of Sorrow",
                      self._error(self._proc(b"ACHE")))

    def test_every_loaded_game_is_named(self) -> None:
        self.assertIn("Circle of the Moon (US) and Harmony of Dissonance (US)",
                      self._error(self._proc(b"AAME", b"ACHE")))

    def test_a_header_away_from_the_allocation_start_is_not_named(self) -> None:
        message = self._error(self._proc(b"AAME", header_offset=0x420))
        self.assertIn("not loaded", message)
        self.assertNotIn("Circle of the Moon", message)

    def test_nothing_loaded_asks_for_the_us_version(self) -> None:
        self.assertRegex(self._error(self._proc()), "not loaded.*US version")

    def test_unpatched_us_rom_points_at_the_installer(self) -> None:
        self.assertRegex(self._error(self._proc(b"A2CE")), "not patched.*ROM Installer")

    def test_seed_from_another_apworld_version_names_both_identifiers(self) -> None:
        message = self._error(self._proc(b"A2CE", marker=b"CVAOS_AP_V0.0"))
        self.assertIn("CVAOS_AP_V0.0", message)
        self.assertIn(ARCHIPELAGO_IDENTIFIER, message)


class EwramSignatureTest(unittest.TestCase):
    def test_accepts_a_valid_ingame_block(self) -> None:
        self.assertTrue(ewram_signature_ok(bytes(_make_ewram_ingame()), 0))

    def test_accepts_at_a_nonzero_offset(self) -> None:
        buf = bytes(_EWRAM_STRUCT_OFFSET) + bytes(_make_ewram_ingame())
        self.assertTrue(ewram_signature_ok(buf, _EWRAM_STRUCT_OFFSET))

    def test_rejects_when_not_ingame(self) -> None:
        self.assertFalse(ewram_signature_ok(bytes(_make_ewram_ingame({GAME_STATE: 0x01})), 0))

    def test_rejects_when_not_normal_menu(self) -> None:
        self.assertFalse(ewram_signature_ok(bytes(_make_ewram_ingame({MENU_STATE: 0x06})), 0))

    def test_rejects_hp_over_maxhp(self) -> None:
        buf = _make_ewram_ingame()
        buf[CURRENT_HP:CURRENT_HP + 2] = (700).to_bytes(2, "little")  # > MaxHP 596
        self.assertFalse(ewram_signature_ok(bytes(buf), 0))

    def test_rejects_zero_maxhp(self) -> None:
        buf = _make_ewram_ingame()
        buf[MAX_HP:MAX_HP + 2] = (0).to_bytes(2, "little")
        buf[CURRENT_HP:CURRENT_HP + 2] = (0).to_bytes(2, "little")
        self.assertFalse(ewram_signature_ok(bytes(buf), 0))

    def test_rejects_insane_gold(self) -> None:
        buf = _make_ewram_ingame()
        buf[CURRENT_GOLD:CURRENT_GOLD + 4] = (39_059_713).to_bytes(4, "little")
        self.assertFalse(ewram_signature_ok(bytes(buf), 0))

    def test_rejects_out_of_range_consumable_count(self) -> None:
        buf = _make_ewram_ingame()
        buf[CONSUMABLE_BASE] = 0x0A  # count of 10 -- impossible in AoS
        self.assertFalse(ewram_signature_ok(bytes(buf), 0))

    def test_rejects_all_zero_block(self) -> None:
        self.assertFalse(ewram_signature_ok(bytes(EWRAM_SIZE), 0))

    def test_rejects_when_buffer_too_short(self) -> None:
        self.assertFalse(ewram_signature_ok(bytes(_make_ewram_ingame())[:0x100], 0))


class WatcherResilienceTest(unittest.TestCase):
    """The collection watcher skips its tick while a seed change is pending, and keeps running
    when a tick raises something other than CollectionError."""

    def test_no_tick_runs_while_a_seed_change_is_pending(self) -> None:
        # Between noticing a different installed seed and reconnecting for it, the connection
        # still belongs to the old seed's slot.
        from types import SimpleNamespace
        from unittest import mock

        from .. import collection_client

        exit_event = asyncio.Event()
        loops: list[int] = []

        def count_loop() -> None:
            loops.append(1)
            if len(loops) == 3:
                exit_event.set()

        tick = mock.AsyncMock()
        ctx = SimpleNamespace(
            exit_event=exit_event,
            backend=CollectionBackend(FakeProc(), rom_base=0, ewram_base=0),
            server=SimpleNamespace(socket=SimpleNamespace(closed=True)),
            slot=1, auth="x", password_requested=False, _seed_changed=True,
            brain=SimpleNamespace(_tick=tick),
            revalidate_anchor=count_loop,
            _detach=mock.Mock(),
        )
        with mock.patch.object(collection_client, "TICK_SECONDS", 0):
            asyncio.run(asyncio.wait_for(collection_client._watcher(ctx), timeout=5))
        tick.assert_not_awaited()

    def test_watcher_survives_an_unexpected_tick_error(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        from .. import collection_client

        ticks: list[int] = []
        exit_event = asyncio.Event()

        async def flaky_tick(ctx, ram):
            ticks.append(1)
            if len(ticks) == 1:
                raise ValueError("boom")
            exit_event.set()

        ctx = SimpleNamespace(
            exit_event=exit_event,
            backend=CollectionBackend(FakeProc(), rom_base=0, ewram_base=0),
            server=SimpleNamespace(socket=SimpleNamespace(closed=True)),
            slot=1, auth="x", password_requested=False, _seed_changed=False,
            brain=SimpleNamespace(_tick=flaky_tick),
            revalidate_anchor=lambda: None,
            _detach=mock.Mock(),
        )
        with mock.patch.object(collection_client, "TICK_SECONDS", 0), \
                mock.patch.object(collection_client, "ERROR_BACKOFF_SECONDS", 0), \
                self.assertLogs("Client", level="ERROR"):
            asyncio.run(asyncio.wait_for(collection_client._watcher(ctx), timeout=5))
        self.assertEqual(len(ticks), 2, "the watcher must tick again after the error")
        ctx._detach.assert_not_called()


class ReattachTest(unittest.IsolatedAsyncioTestCase):
    """Attaching to a collection that holds a different seed reconnects as that seed's slot.

    The old seed's goal and DeathLink latch do not carry over to the new seed. Re-attaching to the
    same seed keeps the connection, the goal and the latch.
    """

    OLD_AUTH, NEW_AUTH = bytes(range(16)), bytes(range(16, 32))

    async def attach_with(self, installed_auth: bytes, currently_dead: bool | None = None, *,
                          finished_game: bool = False, connected: bool = True):
        from unittest import mock

        from .. import collection_client
        from ..rom import AUTH_NUMBER_START
        ctx = collection_client.CVAOSCollectionContext(None, None)
        ctx.slot = 1
        ctx.auth = base64.b64encode(self.OLD_AUTH).decode("ascii")
        ctx.on_package("Connected", {"slot_data": {}})
        ctx.finished_game = finished_game
        if not connected:
            ctx.slot = None
        proc = FakeProc()
        proc.close = lambda: None
        rom = RomImage(0x30000020, 0x30000000, 0x30800000)
        proc.write(rom.base + AUTH_NUMBER_START, installed_auth)
        ctx.brain.currently_dead = currently_dead
        with mock.patch.object(collection_client.GameProcess, "attach", return_value=proc), \
                mock.patch.object(collection_client, "find_rom_image", return_value=rom), \
                mock.patch.object(collection_client, "find_ewram_base", return_value=0x10000170), \
                mock.patch.object(ctx, "disconnect", new=mock.AsyncMock()) as disconnect:
            ctx.try_attach()
            await asyncio.sleep(0)
        return ctx, disconnect

    async def test_a_different_seed_reconnects_as_its_own_slot(self) -> None:
        ctx, disconnect = await self.attach_with(self.NEW_AUTH)
        disconnect.assert_awaited_once_with(allow_autoreconnect=True)
        self.assertIsNotNone(ctx.backend)

    async def test_the_same_seed_keeps_the_connection(self) -> None:
        _ctx, disconnect = await self.attach_with(self.OLD_AUTH)
        disconnect.assert_not_awaited()

    async def test_the_same_seed_keeps_the_deathlink_latch(self) -> None:
        ctx, _disconnect = await self.attach_with(self.OLD_AUTH, currently_dead=True)
        self.assertIs(ctx.brain.currently_dead, True)

    async def test_a_different_seed_does_not_inherit_the_old_seeds_goal(self) -> None:
        # CommonClient reports CLIENT_GOAL on Connected while finished_game is set.
        ctx, _disconnect = await self.attach_with(self.NEW_AUTH, finished_game=True)
        self.assertFalse(ctx.finished_game)

    async def test_a_different_seed_installed_while_disconnected_does_not_inherit_the_goal(self):
        ctx, disconnect = await self.attach_with(self.NEW_AUTH, finished_game=True,
                                                 connected=False)
        self.assertFalse(ctx.finished_game)
        disconnect.assert_not_awaited()

    async def test_the_same_seed_keeps_its_goal(self) -> None:
        ctx, _disconnect = await self.attach_with(self.OLD_AUTH, finished_game=True)
        self.assertTrue(ctx.finished_game)

    async def test_a_reconnect_to_the_same_seed_sends_its_checks_again(self) -> None:
        # A check sent into a connection that was already dead would otherwise never arrive.
        ctx, _disconnect = await self.attach_with(self.OLD_AUTH, currently_dead=True)
        ctx.brain.local_checked_locations = {123}
        ctx.on_package("Connected", {"slot_data": {}})
        self.assertEqual(ctx.brain.local_checked_locations, set())
        self.assertIs(ctx.brain.currently_dead, True, "the DeathLink latch belongs to the game")

    async def test_ticks_wait_for_the_new_seeds_connection(self) -> None:
        ctx, _disconnect = await self.attach_with(self.NEW_AUTH)
        self.assertTrue(ctx._seed_changed)
        ctx.on_package("Connected", {"slot_data": {}})
        self.assertFalse(ctx._seed_changed)

    async def test_a_different_seed_leaves_the_deathlink_latch_unknown(self) -> None:
        ctx, _disconnect = await self.attach_with(self.NEW_AUTH, currently_dead=True)
        self.assertIsNone(ctx.brain.currently_dead)


if __name__ == "__main__":
    unittest.main()
