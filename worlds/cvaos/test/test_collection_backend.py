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
import unittest

from ..collection_client import (
    EWRAM_SIZE, ROM_SIZE, ROM_TITLE, CollectionBackend, CollectionError,
    _EWRAM_STRUCT_OFFSET, _ROM_GAMECODE, _ROM_GAMECODE_OFFSET, _ROM_LOGO_OFFSET,
    _ROM_LOGO_PREFIX, RomImage, ewram_offset, ewram_signature_ok, find_ewram_base,
    validated_rom_base_in,
)
from ..ram import addresses as addr
from ..ram.addresses import EWRAM, GameState

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


def _make_rom_header() -> bytes:
    """Minimal bytes with a valid AoS ROM header at offset 0 (title + logo + game code)."""
    buf = bytearray(0x200)
    buf[ROM_TITLE_OFFSET:ROM_TITLE_OFFSET + len(ROM_TITLE)] = ROM_TITLE
    buf[_ROM_LOGO_OFFSET:_ROM_LOGO_OFFSET + len(_ROM_LOGO_PREFIX)] = _ROM_LOGO_PREFIX
    buf[_ROM_GAMECODE_OFFSET:_ROM_GAMECODE_OFFSET + len(_ROM_GAMECODE)] = _ROM_GAMECODE
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
        header[_ROM_GAMECODE_OFFSET:_ROM_GAMECODE_OFFSET + 4] = b"ACME"  # Circle of the Moon
        self.assertIsNone(validated_rom_base_in(bytes(header), 0))

    def test_no_title_at_all(self) -> None:
        self.assertIsNone(validated_rom_base_in(bytes(0x200), 0))


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
    """A tick that raises something other than CollectionError must not end the watcher."""

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
            slot=1, auth="x", password_requested=False,
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


if __name__ == "__main__":
    unittest.main()
