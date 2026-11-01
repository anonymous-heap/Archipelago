"""
A minimal PNG encoder and decoder for the tracker's generated map images.

They handle only what tracker_map draws: 8-bit RGB or RGBA, one image data chunk, and no row
filtering. That keeps the world free of an imaging dependency.
"""
from __future__ import annotations

import struct
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def encode(width: int, height: int, pixels: bytes, alpha: bool = False) -> bytes:
    """Return a PNG of ``pixels``, given row by row with 3 bytes per pixel (4 with ``alpha``)."""
    stride = width * (4 if alpha else 3)
    if len(pixels) != stride * height:
        raise ValueError("pixel data does not match the image size")
    raw = b"".join(b"\x00" + pixels[row * stride:(row + 1) * stride] for row in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6 if alpha else 2, 0, 0, 0)
    return (SIGNATURE + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(raw, 9))
            + _chunk(b"IEND", b""))


def decode(data: bytes) -> tuple[int, int, bytes]:
    """Return the width, height and pixel bytes of a PNG that :func:`encode` wrote."""
    if not data.startswith(SIGNATURE):
        raise ValueError("not a PNG")
    position, width, height, channels, compressed = len(SIGNATURE), 0, 0, 3, b""
    while position < len(data):
        length, = struct.unpack_from(">I", data, position)
        kind = data[position + 4:position + 8]
        body = data[position + 8:position + 8 + length]
        if kind == b"IHDR":
            width, height, _depth, colour = struct.unpack_from(">IIBB", body)
            channels = 4 if colour == 6 else 3
        elif kind == b"IDAT":
            compressed += body
        position += 12 + length
    raw = zlib.decompress(compressed)
    stride = width * channels
    if any(raw[row * (stride + 1)] != 0 for row in range(height)):
        raise ValueError("only unfiltered rows are supported")
    return width, height, b"".join(raw[row * (stride + 1) + 1:(row + 1) * (stride + 1)]
                                   for row in range(height))
