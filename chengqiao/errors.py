"""Typed business errors.

Every rejected action raises :class:`ChengqiaoError` with a stable ``code`` so
the CLI can translate it into a non-zero exit status and structured stderr.
"""

from __future__ import annotations

from typing import Any


class ChengqiaoError(Exception):
    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict:
        out = {"error": self.code, "message": self.message}
        if self.details is not None:
            out["details"] = self.details
        return out
