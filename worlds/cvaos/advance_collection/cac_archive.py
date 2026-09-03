#!/usr/bin/env python3
"""Pure-Python (stdlib-only) tools for the M2 archive format used by the Steam
release of Castlevania Advance Collection (``windata/alldata.bin`` +
``windata/alldata.psb.m``).

This is an independent, from-scratch implementation of the file format.  The
format itself (MDF container, MT19937-keyed XOR, PSB v3 document) was studied
from public documentation and reference tooling, but no GPL code is reproduced
here.

Format summary
==============

MDF container (files with a ``.m`` suffix)
    ``b"mdf\\0"`` + u32-LE decompressed size + a zlib stream.  Every byte of
    the zlib stream (i.e. everything after the 8-byte header) is XOR'd with a
    repeating 64-byte keystream.  The keystream is derived from
    ``MD5(seed + basename_of_file_lowercased)`` (the basename *includes* the
    ``.m`` suffix): the 16-byte digest is split into four little-endian u32
    words used with MT19937 ``init_by_array``; keystream bytes are successive
    ``genrand_uint32`` outputs packed little-endian.  The seed string for this
    collection is ``"25G/xpvTbsb+6"``.  The original files were deflated with
    standard zlib at level 9 (default memLevel/strategy), which this module
    reproduces byte-exactly.

alldata.bin
    Plain concatenation of member blobs, each starting at a 2048-byte-aligned
    offset (zero padding in between and after the final member).  Members
    whose index name ends in ``.m`` are themselves MDF containers keyed by
    their own (lowercased) basename; other members (e.g. ``system/sound/*``)
    are stored raw.

alldata.psb.m
    MDF container (key name ``alldata.psb.m``) holding a PSB v3 document:
    ``{"expire_suffix_list": [], "file_info": {name: [offset, size], ...},
    "id": "archive", "version": 1.0}``.  ``expire_suffix_list`` must be an
    empty list (not null).  Offsets/sizes are 32-bit ints.

The PSB writer in this module reproduces the original index file
byte-for-byte (same trie packing, integer widths, and section layout).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import struct
import sys
import zlib

DEFAULT_SEED = "25G/xpvTbsb+6"
DEFAULT_KEY_LENGTH = 64
MDF_MAGIC = b"mdf\0"
PSB_MAGIC = b"PSB\0"
BIN_ALIGNMENT = 2048
ZLIB_LEVEL = 9  # reproduces the original archive's compressed streams exactly

# ---------------------------------------------------------------------------
# MT19937 (reference algorithm, including init_by_array)
# ---------------------------------------------------------------------------


class MT19937:
    """Mersenne Twister (mt19937ar) as published by Matsumoto & Nishimura."""

    N = 624
    M = 397
    MATRIX_A = 0x9908B0DF
    UPPER_MASK = 0x80000000
    LOWER_MASK = 0x7FFFFFFF

    def __init__(self, seed=5489):
        self.mt = [0] * self.N
        self.mti = self.N + 1
        if isinstance(seed, (list, tuple)):
            self.init_by_array(seed)
        else:
            self.init_genrand(seed)

    def init_genrand(self, s: int) -> None:
        mt = self.mt
        mt[0] = s & 0xFFFFFFFF
        for i in range(1, self.N):
            mt[i] = (1812433253 * (mt[i - 1] ^ (mt[i - 1] >> 30)) + i) & 0xFFFFFFFF
        self.mti = self.N

    def init_by_array(self, init_key) -> None:
        self.init_genrand(19650218)
        mt = self.mt
        key_length = len(init_key)
        i, j = 1, 0
        k = max(self.N, key_length)
        for _ in range(k):
            mt[i] = ((mt[i] ^ ((mt[i - 1] ^ (mt[i - 1] >> 30)) * 1664525)) + init_key[j] + j) & 0xFFFFFFFF
            i += 1
            j += 1
            if i >= self.N:
                mt[0] = mt[self.N - 1]
                i = 1
            if j >= key_length:
                j = 0
        for _ in range(self.N - 1):
            mt[i] = ((mt[i] ^ ((mt[i - 1] ^ (mt[i - 1] >> 30)) * 1566083941)) - i) & 0xFFFFFFFF
            i += 1
            if i >= self.N:
                mt[0] = mt[self.N - 1]
                i = 1
        mt[0] = 0x80000000

    def genrand_uint32(self) -> int:
        mt = self.mt
        if self.mti >= self.N:
            if self.mti == self.N + 1:
                self.init_genrand(5489)
            for kk in range(self.N - self.M):
                y = (mt[kk] & self.UPPER_MASK) | (mt[kk + 1] & self.LOWER_MASK)
                mt[kk] = mt[kk + self.M] ^ (y >> 1) ^ (self.MATRIX_A if y & 1 else 0)
            for kk in range(self.N - self.M, self.N - 1):
                y = (mt[kk] & self.UPPER_MASK) | (mt[kk + 1] & self.LOWER_MASK)
                mt[kk] = mt[kk + (self.M - self.N)] ^ (y >> 1) ^ (self.MATRIX_A if y & 1 else 0)
            y = (mt[self.N - 1] & self.UPPER_MASK) | (mt[0] & self.LOWER_MASK)
            mt[self.N - 1] = mt[self.M - 1] ^ (y >> 1) ^ (self.MATRIX_A if y & 1 else 0)
            self.mti = 0
        y = mt[self.mti]
        self.mti += 1
        y ^= y >> 11
        y ^= (y << 7) & 0x9D2C5680
        y ^= (y << 15) & 0xEFC60000
        y ^= y >> 18
        return y


# ---------------------------------------------------------------------------
# MDF container crypto
# ---------------------------------------------------------------------------


def _key_basename(filename: str) -> str:
    """Return the lowercased basename used for key derivation.

    The name must include the ``.m`` suffix for MDF-wrapped members, e.g.
    ``alldata.psb.m`` or ``03_akatsuki_us.patch_210623m.bin.m``.
    """
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    return name.lower()


def derive_keystream(filename: str, seed: str = DEFAULT_SEED,
                     key_length: int = DEFAULT_KEY_LENGTH) -> bytes:
    """Derive the repeating XOR keystream for *filename*."""
    digest = hashlib.md5((seed + _key_basename(filename)).encode("utf-8")).digest()
    words = struct.unpack("<4I", digest)
    twister = MT19937(list(words))
    out = bytearray()
    while len(out) < key_length:
        out += struct.pack("<I", twister.genrand_uint32())
    return bytes(out[:key_length])


def _xor_keystream(data: bytes, key: bytes) -> bytes:
    """XOR *data* with the repeating keystream *key* (period ``len(key)``)."""
    klen = len(key)
    if not data:
        return b""
    # Tile the key across the whole buffer and XOR in bulk.
    reps = -(-len(data) // klen)
    tiled = (key * reps)[: len(data)]
    return (int.from_bytes(data, "little") ^ int.from_bytes(tiled, "little")).to_bytes(len(data), "little")


def mdf_decrypt(filename: str, data: bytes, seed: str = DEFAULT_SEED,
                key_length: int = DEFAULT_KEY_LENGTH) -> bytes:
    """Decrypt + inflate an MDF container; returns the plain content."""
    if data[:4] != MDF_MAGIC:
        raise ValueError(f"{filename!r}: not an MDF container (magic {data[:4]!r})")
    (expected_len,) = struct.unpack_from("<I", data, 4)
    key = derive_keystream(filename, seed, key_length)
    stream = _xor_keystream(data[8:], key)
    plain = zlib.decompress(stream)
    if len(plain) != expected_len:
        raise ValueError(
            f"{filename!r}: decompressed size {len(plain)} != header size {expected_len}")
    return plain


def mdf_encrypt(filename: str, data: bytes, seed: str = DEFAULT_SEED,
                key_length: int = DEFAULT_KEY_LENGTH, level: int = ZLIB_LEVEL) -> bytes:
    """Deflate + encrypt plain content into an MDF container."""
    stream = zlib.compress(data, level)
    key = derive_keystream(filename, seed, key_length)
    return MDF_MAGIC + struct.pack("<I", len(data)) + _xor_keystream(stream, key)


# ---------------------------------------------------------------------------
# PSB v3 reader
# ---------------------------------------------------------------------------

# Type ids:
#  1 null | 2 true | 3 false
#  4 int 0 | 5..8 int, 1..4 bytes (signed LE)
#  9..12 long, 5..8 bytes (signed LE)
#  13..16 uint, 1..4 bytes (unsigned LE; used for counts/indexes)
#  17..20 key-name index (v1 objects only)
#  21..24 string-table index
#  25..28 stream index | 34..37 b-stream index (not used by the archive index)
#  29 float 0.0 | 30 float32 | 31 double
#  32 array | 33 object


class PsbError(ValueError):
    pass


class _PsbParser:
    def __init__(self, data: bytes):
        self.data = data
        if data[:4] != PSB_MAGIC:
            raise PsbError("not a PSB file")
        self.version, self.flags = struct.unpack_from("<HH", data, 4)
        if self.version < 2:
            raise PsbError("PSB v1 not supported by this reader")
        if self.flags & 0x3:
            raise PsbError("filtered (encrypted) PSB not supported")
        (self.keys_offsets_offset, self.keys_blob_offset,
         self.strings_offsets_offset, self.strings_blob_offset,
         self.streams_offsets_offset, self.streams_sizes_offset,
         self.streams_blob_offset, self.root_offset) = struct.unpack_from("<8I", data, 8)
        if self.version >= 3:
            (checksum,) = struct.unpack_from("<I", data, 40)
            calc = zlib.adler32(data[8:40])
            if self.version >= 4:
                calc = zlib.adler32(data[44:56], calc)
            if calc != checksum:
                raise PsbError("header checksum mismatch")
        self._load_key_names()
        self._load_strings()

    # -- primitives ---------------------------------------------------------

    def _read_uint(self, pos: int, type_id: int):
        n = type_id - 12  # 13..16 -> 1..4 bytes
        if not 1 <= n <= 4:
            raise PsbError(f"bad uint type id {type_id} at 0x{pos:x}")
        return int.from_bytes(self.data[pos:pos + n], "little"), pos + n

    def _read_uint_array(self, pos: int):
        count, pos = self._read_uint(pos + 1, self.data[pos])
        vtid = self.data[pos]
        pos += 1
        n = vtid - 12
        if not 1 <= n <= 4:
            raise PsbError(f"bad uint array value type id {vtid}")
        end = pos + count * n
        raw = self.data[pos:end]
        vals = [int.from_bytes(raw[i:i + n], "little") for i in range(0, len(raw), n)]
        return vals, end

    def _read_cstring(self, pos: int) -> str:
        end = self.data.index(b"\0", pos)
        return self.data[pos:end].decode("utf-8")

    # -- tables -------------------------------------------------------------

    def _load_key_names(self):
        pos = self.keys_blob_offset
        value_offsets, pos = self._read_uint_array(pos)
        tree, pos = self._read_uint_array(pos)
        tails, pos = self._read_uint_array(pos)
        names = []
        for tail in tails:
            buf = bytearray()
            cur = tree[tail]  # skip the terminal (NUL) node itself
            while cur != 0:
                parent = tree[cur]
                buf.append(cur - value_offsets[parent])
                cur = parent
            names.append(bytes(reversed(buf)).decode("utf-8"))
        self.key_names = names

    def _load_strings(self):
        offsets, _ = self._read_uint_array(self.strings_offsets_offset)
        self.strings = [self._read_cstring(self.strings_blob_offset + o) for o in offsets]

    # -- tokens -------------------------------------------------------------

    def read_root(self):
        return self._read_token(self.root_offset)[0]

    def _read_token(self, pos: int):
        t = self.data[pos]
        pos += 1
        if t == 1:
            return None, pos
        if t == 2:
            return True, pos
        if t == 3:
            return False, pos
        if t == 4:
            return 0, pos
        if 5 <= t <= 8:
            n = t - 4
            return int.from_bytes(self.data[pos:pos + n], "little", signed=True), pos + n
        if 9 <= t <= 12:
            n = t - 4
            return int.from_bytes(self.data[pos:pos + n], "little", signed=True), pos + n
        if 21 <= t <= 24:
            idx, pos = self._read_uint(pos, t - 8)
            return self.strings[idx], pos
        if t == 29:
            return 0.0, pos
        if t == 30:
            return struct.unpack_from("<f", self.data, pos)[0], pos + 4
        if t == 31:
            return struct.unpack_from("<d", self.data, pos)[0], pos + 8
        if t == 32:
            offsets, pos = self._read_uint_array(pos)
            base = pos
            return [self._read_token(base + o)[0] for o in offsets], pos
        if t == 33:
            key_indexes, pos = self._read_uint_array(pos)
            offsets, pos = self._read_uint_array(pos)
            base = pos
            obj = {}
            for ki, o in zip(key_indexes, offsets):
                obj[self.key_names[ki]] = self._read_token(base + o)[0]
            return obj, pos
        raise PsbError(f"unsupported PSB token type {t} at 0x{pos - 1:x}")


def psb_read(data: bytes):
    """Parse a PSB v2/v3 document into plain Python objects."""
    return _PsbParser(data).read_root()


# ---------------------------------------------------------------------------
# PSB v3 writer (byte-exact against the collection's original index)
# ---------------------------------------------------------------------------


def _ordinal_key(s: str) -> bytes:
    # Ordinal (bytewise) sort; all names in this archive are ASCII.
    return s.encode("utf-8")


class _TrieNode:
    __slots__ = ("char", "children", "index", "parent_index", "value_offset",
                 "terminal", "tail_index")

    def __init__(self, char: int, terminal: bool = False):
        self.char = char
        self.children = {}
        self.index = 0
        self.parent_index = 0
        self.value_offset = 0
        self.terminal = terminal
        self.tail_index = 0


class _KeyNameTrie:
    """Builds the PSB key-name trie and packs node indexes the same way the
    game's original index is packed (compact first-fit range allocation)."""

    def __init__(self, names):
        self.names = sorted(set(names), key=_ordinal_key)
        self.lookup = {n: i for i, n in enumerate(self.names)}
        self.root = _TrieNode(0)
        self.node_count = 1
        for idx, name in enumerate(self.names):
            self._insert(name, idx)
        self._pack()

    def _insert(self, name: str, index: int) -> None:
        node = self.root
        for b in name.encode("utf-8"):
            child = node.children.get(b)
            if child is None:
                child = _TrieNode(b)
                node.children[b] = child
                self.node_count += 1
            node = child
        term = _TrieNode(0, terminal=True)
        term.tail_index = index
        node.children[0] = term
        self.node_count += 1

    def _pack(self) -> None:
        used = [False] * self.node_count
        used[0] = True
        self.root.index = 0
        min_free = 1

        def find_free_range(min_slot, rng):
            i = min_slot
            while i < len(used):
                found = True
                need_ext = False
                for o in rng:
                    t = i + o
                    if t < len(used):
                        if used[t]:
                            found = False
                            break
                    else:
                        need_ext = True
                        break
                if found:
                    return i, need_ext
                i += 1
            return max(len(used), min_slot), True

        stack = [self.root]
        while stack:
            node = stack.pop()
            children = sorted(node.children.values(), key=lambda c: c.char)
            if not children:
                continue
            min_child = children[0].char
            rng = [c.char - min_child for c in children]
            base, need_ext = find_free_range(max(min_free, min_child + 1), rng)
            if need_ext:
                target = base + rng[-1]
                used.extend([False] * (target + 1 - len(used)))
            for child, off in zip(children, rng):
                child.index = base + off
                child.parent_index = node.index
                used[child.index] = True
            node.value_offset = base - min_child
            while min_free < len(used) and used[min_free]:
                min_free += 1
            # depth-first, children in ascending character order
            stack.extend(c for c in reversed(children) if not c.terminal)

        total = len(used)
        self.value_offsets = [0] * total
        self.tree = [0] * total
        self.tails = [0] * len(self.names)

        # iterative walk (avoids recursion limits on deep names)
        walk = [self.root]
        while walk:
            node = walk.pop()
            self.tree[node.index] = node.parent_index
            if node.terminal:
                self.tails[node.tail_index] = node.index
                self.value_offsets[node.index] = node.tail_index
            else:
                self.value_offsets[node.index] = node.value_offset
                walk.extend(node.children.values())


def _uint_width(value: int) -> int:
    if value < 0:
        raise PsbError("negative uint")
    n = 0
    while value:
        n += 1
        value >>= 8
    return max(n, 1)


def _int_width(value: int) -> int:
    if -(1 << 7) <= value < (1 << 7):
        return 1
    if -(1 << 15) <= value < (1 << 15):
        return 2
    if -(1 << 23) <= value < (1 << 23):
        return 3
    return 4


def _write_uint(buf: bytearray, value: int, base_id: int) -> None:
    n = _uint_width(value)
    buf.append(base_id + n - 1)
    buf += value.to_bytes(n, "little")


def _write_uint_array(buf: bytearray, values) -> None:
    width = 1
    for v in values:
        w = _uint_width(v)
        if w > width:
            width = w
            if width == 4:
                break
    _write_uint(buf, len(values), 13)
    buf.append(13 + width - 1)
    for v in values:
        buf += v.to_bytes(width, "little")


class _PsbBuilder:
    def __init__(self, root, version: int = 3):
        if version != 3:
            raise PsbError("only PSB v3 writing is supported")
        self.version = version
        self.root = root
        names = []
        strings = []
        self._collect(root, names, strings)
        self.trie = _KeyNameTrie(names)
        self.strings = sorted(set(strings), key=_ordinal_key)
        self.string_lookup = {s: i for i, s in enumerate(self.strings)}

    def _collect(self, node, names, strings) -> None:
        if isinstance(node, str):
            strings.append(node)
        elif isinstance(node, dict):
            for k, v in node.items():
                names.append(k)
                self._collect(v, names, strings)
        elif isinstance(node, (list, tuple)):
            for v in node:
                self._collect(v, names, strings)

    def build(self) -> bytes:
        body = bytearray()
        # key names (offsets table and blob coincide for v>=2)
        keys_offset = 44
        _write_uint_array(body, self.trie.value_offsets)
        _write_uint_array(body, self.trie.tree)
        _write_uint_array(body, self.trie.tails)
        root_offset = keys_offset + len(body)
        self._write_token(body, self.root)
        strings_offsets_offset = keys_offset + len(body)
        blob = bytearray()
        offsets = []
        for s in self.strings:
            offsets.append(len(blob))
            blob += s.encode("utf-8") + b"\0"
        _write_uint_array(body, offsets)
        strings_blob_offset = keys_offset + len(body)
        body += blob
        streams_offsets_offset = keys_offset + len(body)
        _write_uint_array(body, [])
        streams_sizes_offset = keys_offset + len(body)
        _write_uint_array(body, [])
        streams_blob_offset = keys_offset + len(body)

        header_fields = struct.pack(
            "<8I", keys_offset, keys_offset, strings_offsets_offset,
            strings_blob_offset, streams_offsets_offset, streams_sizes_offset,
            streams_blob_offset, root_offset)
        checksum = zlib.adler32(header_fields)
        return PSB_MAGIC + struct.pack("<HH", 3, 0) + header_fields + struct.pack("<I", checksum) + bytes(body)

    # -- tokens -------------------------------------------------------------

    def _write_token(self, buf: bytearray, node) -> None:
        if node is None:
            buf.append(1)
        elif node is True:
            buf.append(2)
        elif node is False:
            buf.append(3)
        elif isinstance(node, int):
            self._write_int(buf, node)
        elif isinstance(node, float):
            self._write_float(buf, node)
        elif isinstance(node, str):
            _write_uint(buf, self.string_lookup[node], 21)
        elif isinstance(node, (list, tuple)):
            self._write_array(buf, node)
        elif isinstance(node, dict):
            self._write_object(buf, node)
        else:
            raise PsbError(f"cannot serialize {type(node).__name__} to PSB")

    def _write_int(self, buf: bytearray, value: int) -> None:
        if -(1 << 31) <= value < (1 << 31):
            if value == 0:
                buf.append(4)
                return
            n = _int_width(value)
            buf.append(4 + n)
            buf += (value & 0xFFFFFFFF).to_bytes(4, "little")[:n]
        else:
            if not -(1 << 63) <= value < (1 << 63):
                raise PsbError(f"integer out of 64-bit range: {value}")
            n = _int_width(value >> 32) + 4
            buf.append(9 + n - 5)
            buf += (value & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little")[:n]

    def _write_float(self, buf: bytearray, value: float) -> None:
        if value == 0 and not (value != value):  # 0.0 / -0.0
            buf.append(29)
            return
        packed = struct.pack("<f", value)
        if struct.unpack("<f", packed)[0] == value:
            buf.append(30)
            buf += packed
        else:
            buf.append(31)
            buf += struct.pack("<d", value)

    def _write_array(self, buf: bytearray, items) -> None:
        buf.append(32)
        blob = bytearray()
        offsets = []
        cache = {}
        for item in items:
            piece = bytearray()
            self._write_token(piece, item)
            piece = bytes(piece)
            prev = cache.get(piece)
            if prev is not None:
                offsets.append(prev)
            else:
                cache[piece] = len(blob)
                offsets.append(len(blob))
                blob += piece
        _write_uint_array(buf, offsets)
        buf += blob

    def _write_object(self, buf: bytearray, obj) -> None:
        buf.append(33)
        entries = sorted(obj.items(), key=lambda kv: _ordinal_key(kv[0]))
        blob = bytearray()
        offsets = []
        key_indexes = []
        cache = {}
        for key, value in entries:
            key_indexes.append(self.trie.lookup[key])
            piece = bytearray()
            self._write_token(piece, value)
            piece = bytes(piece)
            prev = cache.get(piece)
            if prev is not None:
                offsets.append(prev)
            else:
                cache[piece] = len(blob)
                offsets.append(len(blob))
                blob += piece
        _write_uint_array(buf, key_indexes)
        _write_uint_array(buf, offsets)
        buf += blob


def psb_write(root, version: int = 3) -> bytes:
    """Serialize plain Python objects to a PSB v3 document."""
    return _PsbBuilder(root, version).build()


# ---------------------------------------------------------------------------
# Archive-level operations
# ---------------------------------------------------------------------------


def load_index(psb_m_path: str, index_key_name: str | None = None):
    """Decrypt/parse ``alldata.psb.m``; returns the root object (a dict).

    *index_key_name* names the file for key derivation when it is not stored
    under the name the game knows it by (a backup copy, say); the default is
    the file's own basename."""
    with open(psb_m_path, "rb") as f:
        data = f.read()
    if data[:4] == MDF_MAGIC:
        data = mdf_decrypt(index_key_name or os.path.basename(psb_m_path), data)
    root = psb_read(data)
    if not isinstance(root, dict) or "file_info" not in root:
        raise PsbError("PSB does not look like an archive index")
    return root


def list_members(psb_m_path: str):
    """Return ``[(stored_name, offset, size), ...]`` in ascending offset order."""
    fi = load_index(psb_m_path)["file_info"]
    return sorted(((name, off, size) for name, (off, size) in fi.items()),
                  key=lambda t: t[1])


def _resolve_member(file_info: dict, member_name: str) -> str:
    name = member_name.replace("\\", "/")
    if name in file_info:
        return name
    if name + ".m" in file_info:
        return name + ".m"
    raise KeyError(f"member {member_name!r} not found in archive index")


def extract_member(psb_m_path: str, bin_path: str, member_name: str,
                   raw: bool = False, index_key_name: str | None = None) -> bytes:
    """Extract one member; returns decoded plain bytes (or the stored blob if
    *raw* is true or the member is not MDF-wrapped).  *index_key_name* is
    passed to :func:`load_index`."""
    fi = load_index(psb_m_path, index_key_name)["file_info"]
    name = _resolve_member(fi, member_name)
    off, size = fi[name]
    with open(bin_path, "rb") as f:
        f.seek(off)
        blob = f.read(size)
    if len(blob) != size:
        raise ValueError(f"short read for {name!r}")
    if raw or not (name.endswith(".m") and blob[:4] == MDF_MAGIC):
        return blob
    return mdf_decrypt(name, blob)


def unpack(psb_m_path: str, bin_path: str, out_dir: str, members=None,
           raw: bool = False, verbose: bool = False):
    """Extract all (or *members*) entries from the archive into *out_dir*.

    By default MDF-wrapped members (``*.m``) are decrypted+inflated and
    written without the ``.m`` suffix; other members are copied as stored.
    With ``raw=True`` every member is written as stored, keeping its name.
    Returns the list of file paths written.
    """
    fi = load_index(psb_m_path)["file_info"]
    if members is None:
        selected = sorted(fi.items(), key=lambda kv: kv[1][0])
    else:
        selected = []
        for m in members:
            name = _resolve_member(fi, m)
            selected.append((name, fi[name]))
    written = []
    with open(bin_path, "rb") as f:
        for name, (off, size) in selected:
            f.seek(off)
            blob = f.read(size)
            if len(blob) != size:
                raise ValueError(f"short read for {name!r}")
            out_name = name
            if not raw and name.endswith(".m") and blob[:4] == MDF_MAGIC:
                blob = mdf_decrypt(name, blob)
                out_name = name[:-2]
            out_path = os.path.join(out_dir, *out_name.split("/"))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "wb") as of:
                of.write(blob)
            written.append(out_path)
            if verbose:
                print(f"  {name} -> {out_path} ({len(blob)} bytes)")
    return written


def _align(n: int, alignment: int = BIN_ALIGNMENT) -> int:
    return (n + alignment - 1) // alignment * alignment


def replace_member(alldata_psb_m: str, alldata_bin: str, member_name: str,
                   new_plain_bytes: bytes, out_psb_m: str, out_bin: str,
                   verbose: bool = False,
                   index_key_name: str = "alldata.psb.m") -> None:
    """Rebuild the archive with one member replaced.

    Every other member's stored bytes are copied through untouched; only the
    replaced member is recompressed/re-encrypted (if it is an ``.m`` member).
    The new ``alldata.bin`` keeps the original member order and 2048-byte
    alignment, and the regenerated encrypted index is written to *out_psb_m*.
    Replacing a member with content identical to the original reproduces both
    files byte-for-byte.

    *index_key_name* is the filename used to derive the index's encryption
    key.  It must match the name the file will have when the game loads it
    (``alldata.psb.m``), NOT the temporary output filename.
    """
    root = load_index(alldata_psb_m)
    fi = root["file_info"]
    target = _resolve_member(fi, member_name)

    if target.endswith(".m"):
        new_blob = mdf_encrypt(target, new_plain_bytes)
    else:
        new_blob = bytes(new_plain_bytes)

    order = sorted(fi.items(), key=lambda kv: kv[1][0])
    new_file_info = dict(fi)  # keep original dict for non-layout keys
    pos = 0
    chunk = 1 << 20
    with open(alldata_bin, "rb") as src, open(out_bin, "wb") as dst:
        for name, (off, size) in order:
            start = _align(pos)
            if start > pos:
                dst.write(b"\0" * (start - pos))
                pos = start
            if name == target:
                dst.write(new_blob)
                new_size = len(new_blob)
            else:
                src.seek(off)
                remaining = size
                while remaining:
                    piece = src.read(min(chunk, remaining))
                    if not piece:
                        raise ValueError(f"short read for {name!r}")
                    dst.write(piece)
                    remaining -= len(piece)
                new_size = size
            new_file_info[name] = [pos, new_size]
            if verbose:
                print(f"  {name}: offset {pos}, size {new_size}"
                      + (" (replaced)" if name == target else ""))
            pos += new_size
        end = _align(pos)
        if end > pos:
            dst.write(b"\0" * (end - pos))

    new_root = dict(root)
    new_root["file_info"] = new_file_info
    psb = psb_write(new_root)
    with open(out_psb_m, "wb") as f:
        f.write(mdf_encrypt(index_key_name, psb))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_list(args):
    total = 0
    for name, off, size in list_members(args.psb_m):
        print(f"{off:>12} {size:>10}  {name}")
        total += 1
    print(f"{total} members")


def _cli_dump(args):
    with open(args.file, "rb") as f:
        data = f.read()
    if data[:4] == MDF_MAGIC:
        data = mdf_decrypt(os.path.basename(args.file), data)
    print(json.dumps(psb_read(data), indent=2))


def _cli_unpack(args):
    files = unpack(args.psb_m, args.bin, args.out_dir, members=args.member or None,
                   raw=args.raw, verbose=True)
    print(f"wrote {len(files)} files")


def _cli_extract(args):
    data = extract_member(args.psb_m, args.bin, args.member, raw=args.raw)
    with open(args.out_file, "wb") as f:
        f.write(data)
    print(f"wrote {len(data)} bytes to {args.out_file}")


def _cli_replace(args):
    with open(args.new_file, "rb") as f:
        new_plain = f.read()
    replace_member(args.psb_m, args.bin, args.member, new_plain,
                   args.out_psb_m, args.out_bin, verbose=True)
    print(f"wrote {args.out_bin} and {args.out_psb_m}")


def _cli_decrypt(args):
    with open(args.file_m, "rb") as f:
        data = f.read()
    name = args.name or os.path.basename(args.file_m)
    plain = mdf_decrypt(name, data)
    with open(args.out_file, "wb") as f:
        f.write(plain)
    print(f"wrote {len(plain)} bytes to {args.out_file}")


def _cli_encrypt(args):
    with open(args.file, "rb") as f:
        data = f.read()
    name = args.name or os.path.basename(args.file) + ".m"
    blob = mdf_encrypt(name, data)
    with open(args.out_file, "wb") as f:
        f.write(blob)
    print(f"wrote {len(blob)} bytes to {args.out_file} (key name {name!r})")


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Castlevania Advance Collection archive tool "
                    "(alldata.bin / alldata.psb.m), pure Python.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="list archive members")
    sp.add_argument("psb_m", help="path to alldata.psb.m")
    sp.set_defaults(func=_cli_list)

    sp = sub.add_parser("dump", help="dump a PSB (or .psb.m) as JSON")
    sp.add_argument("file")
    sp.set_defaults(func=_cli_dump)

    sp = sub.add_parser("unpack", help="extract members (decrypt+inflate .m members)")
    sp.add_argument("psb_m")
    sp.add_argument("bin")
    sp.add_argument("out_dir")
    sp.add_argument("--member", action="append", help="extract only this member (repeatable)")
    sp.add_argument("--raw", action="store_true", help="write stored blobs without decoding")
    sp.set_defaults(func=_cli_unpack)

    sp = sub.add_parser("extract", help="extract a single member to a file")
    sp.add_argument("psb_m")
    sp.add_argument("bin")
    sp.add_argument("member")
    sp.add_argument("out_file")
    sp.add_argument("--raw", action="store_true")
    sp.set_defaults(func=_cli_extract)

    sp = sub.add_parser("replace", help="rebuild archive with one member replaced")
    sp.add_argument("psb_m")
    sp.add_argument("bin")
    sp.add_argument("member", help="index name, e.g. system/roms/03_Akatsuki_US.patch_210623m.bin[.m]")
    sp.add_argument("new_file", help="plain (decompressed) replacement content")
    sp.add_argument("out_psb_m")
    sp.add_argument("out_bin")
    sp.set_defaults(func=_cli_replace)

    sp = sub.add_parser("decrypt", help="decrypt+inflate a single .m file")
    sp.add_argument("file_m")
    sp.add_argument("out_file")
    sp.add_argument("--name", help="override key-derivation filename (default: basename)")
    sp.set_defaults(func=_cli_decrypt)

    sp = sub.add_parser("encrypt", help="deflate+encrypt a file into an MDF .m container")
    sp.add_argument("file")
    sp.add_argument("out_file")
    sp.add_argument("--name", help="override key-derivation filename (default: basename + '.m')")
    sp.set_defaults(func=_cli_encrypt)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
