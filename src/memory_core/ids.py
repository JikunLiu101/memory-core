"""Time-sortable unique IDs (ULID: 48-bit millisecond timestamp + 80 random bits, Crockford base32)."""

from __future__ import annotations

import os
import threading
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RAND_LIMIT = 1 << 80

_lock = threading.Lock()
_last_ms = -1
_last_rand = 0


def new_id() -> str:
    """Return a new ULID. IDs created in this process are strictly increasing."""
    global _last_ms, _last_rand
    with _lock:
        ms = int(time.time() * 1000)
        if ms <= _last_ms:
            # Same millisecond (or the clock went back): keep order by incrementing.
            ms = _last_ms
            _last_rand += 1
            if _last_rand >= _RAND_LIMIT:
                ms += 1
                _last_rand = 0
        else:
            _last_rand = int.from_bytes(os.urandom(10), "big")
        _last_ms = ms
        value = (ms << 80) | _last_rand
    return _encode(value)


def _encode(value: int) -> str:
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))
