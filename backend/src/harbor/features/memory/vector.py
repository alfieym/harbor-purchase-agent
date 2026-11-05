"""Float32 vector encoding + cosine similarity for the S8 memory store.

The long-term memory keeps each preference's embedding in the same SQLite row as
its text (same transaction), so the on-disk form is a plain ``BLOB`` of
little-endian float32 values. ``struct`` is the whole encoder — no dependency.
"""

import math
import struct
from collections.abc import Sequence


def pack(values: Sequence[float]) -> bytes:
    """Encode ``values`` as little-endian float32 (4 bytes per value)."""
    return struct.pack(f"<{len(values)}f", *values)


def unpack(blob: bytes) -> list[float]:
    """Decode a float32 ``BLOB`` written by :func:`pack`."""
    if len(blob) % 4 != 0:
        raise ValueError(f"vector blob has {len(blob)} bytes, not a multiple of 4")
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity, with degenerate inputs mapped to safe values.

    A dimension mismatch returns ``-1.0`` (an impossible similarity, so callers
    can filter it out) and a zero vector returns ``0.0`` — both happen after an
    embedding-model change, and neither should raise inside a retrieval loop.
    """
    if len(a) != len(b) or not a:
        return -1.0
    dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(math.fsum(x * x for x in a))
    norm_b = math.sqrt(math.fsum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
