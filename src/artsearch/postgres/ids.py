from __future__ import annotations

import secrets
import time
import uuid


def uuid7(*, timestamp_ms: int | None = None, random_bits: int | None = None) -> uuid.UUID:
    """Generate an RFC 9562 UUIDv7 without requiring PostgreSQL 18."""

    milliseconds = time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms
    if not 0 <= milliseconds < 1 << 48:
        raise ValueError("timestamp_ms must fit in the 48-bit UUIDv7 timestamp field")
    randomness = secrets.randbits(74) if random_bits is None else random_bits
    if not 0 <= randomness < 1 << 74:
        raise ValueError("random_bits must fit in 74 bits")

    random_a = randomness >> 62
    random_b = randomness & ((1 << 62) - 1)
    value = (milliseconds << 80) | (0x7 << 76) | (random_a << 64) | (0b10 << 62) | random_b
    return uuid.UUID(int=value)
