"""Canonical JSON and digest helpers.

Digests make the processing-end submission self-verifying on the account end
without either database reading the other one.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(obj: Any) -> str:
    """Stable JSON text: sorted object keys, compact separators, UTF-8 text."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def is_nonneg_int(value: Any) -> bool:
    # bool is an int subclass; grams/limits must be real integers.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value != ""
