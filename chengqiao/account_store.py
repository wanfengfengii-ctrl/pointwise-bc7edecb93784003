"""Account store: declaration, endpoint receipt posting and sealing.

A second, independent SQLite file.  The account end never opens the
processing store and never reads its tables.  It accepts only originals
produced by the processing end:

* a submission receipt (the manifest of expected endpoint paths), and
* individual endpoint pieces (one occurrence each), each bound to a
  submission id.

The account end only books pieces whose submission has been declared; before
declaration there is nothing to bind them to, so they are refused as
``not-declared`` rather than guessed into a receipt.

Trust comes from the documents themselves: every piece and every manifest
carries a digest over its canonical text, and expectations are re-derived from
the manifest text.  A piece bound to a different submission is ``misbound``;
a piece whose text differs from the declared expectation (including being for
a path that was never declared) is a ``text-conflict``.

Posting is order-independent and idempotent.  An occurrence is keyed by
``(receipt_id, orig_no, path)`` and booked at most once.  Sealing runs in a
single transaction over account material only: if any expected path is
missing it changes nothing and reports ``pending``; a repeated seal returns
the one result already committed.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import model
from .canon import canonical
from .errors import ChengqiaoError
from .rules import EDGE_ORDER
from .sqlite_util import close, connect, transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS declared (
    receipt_id      TEXT PRIMARY KEY,
    submission_id   TEXT NOT NULL UNIQUE,
    rule_id         TEXT NOT NULL,
    batch           TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    declared_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS expected (
    receipt_id   TEXT NOT NULL,
    orig_no      TEXT NOT NULL,
    path_key     TEXT NOT NULL,
    orig_index   INTEGER NOT NULL,
    category     TEXT NOT NULL,
    grams        INTEGER NOT NULL,
    piece_id     TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (receipt_id, orig_no, path_key)
);
CREATE TABLE IF NOT EXISTS received (
    receipt_id   TEXT NOT NULL,
    orig_no      TEXT NOT NULL,
    path_key     TEXT NOT NULL,
    piece_id     TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    piece_json   TEXT NOT NULL,
    received_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (receipt_id, orig_no, path_key)
);
CREATE TABLE IF NOT EXISTS seals (
    result_id       TEXT PRIMARY KEY,
    receipt_id      TEXT NOT NULL UNIQUE REFERENCES declared(receipt_id),
    manifest_digest TEXT NOT NULL,
    result_json     TEXT NOT NULL,
    sealed_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id  TEXT,
    kind        TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


def _load(text: str) -> Any:
    return json.loads(text)


def _path_sort_key(path_key: str) -> tuple[int, ...]:
    return tuple(EDGE_ORDER[edge] for edge in path_key)


class AccountStore:
    """Independent account ledger: declaration, posting, sealing, lookup."""

    def __init__(self, path: str):
        self.path = path
        self._bootstrap()

    # ------------------------------------------------------------- plumbing
    def _bootstrap(self) -> None:
        conn = connect(self.path)
        try:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')"
            )
        finally:
            close(conn)

    def _conn(self) -> sqlite3.Connection:
        conn = connect(self.path)
        conn.executescript(SCHEMA)
        return conn

    def close(self) -> None:
        """No persistent handle is kept; provided so callers can be explicit."""
        return None

    @staticmethod
    def _event(conn, receipt_id, kind: str, detail: dict) -> None:
        conn.execute(
            "INSERT INTO events(receipt_id, kind, detail_json) VALUES (?, ?, ?)",
            (receipt_id, kind, canonical(detail)),
        )

    # ----------------------------------------------------------- declaration
    def declare(self, payload: dict) -> dict:
        """Accept one processing-end submission receipt (the manifest)."""
        doc = model.validate_manifest_payload(payload)
        receipt_id = doc["receipt_id"]
        payload_json = canonical(payload)

        conn = self._conn()
        try:
            with transaction(conn):
                existing = conn.execute(
                    "SELECT submission_id, manifest_digest, payload_json FROM declared"
                    " WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone()
                if existing is not None:
                    if existing["payload_json"] != payload_json:
                        raise ChengqiaoError(
                            "text-conflict",
                            "receipt was already declared with different text",
                            {"receipt_id": receipt_id},
                        )
                    return {
                        "receipt_id": receipt_id,
                        "submission_id": existing["submission_id"],
                        "manifest_digest": existing["manifest_digest"],
                        "replayed": True,
                        "status": self._status(conn, receipt_id),
                    }

                clash = conn.execute(
                    "SELECT receipt_id FROM declared WHERE submission_id = ?",
                    (doc["submission_id"],),
                ).fetchone()
                if clash is not None:
                    raise ChengqiaoError(
                        "misbound",
                        "submission is already bound to a different receipt",
                        {
                            "submission_id": doc["submission_id"],
                            "bound_receipt_id": clash["receipt_id"],
                        },
                    )

                conn.execute(
                    "INSERT INTO declared(receipt_id, submission_id, rule_id, batch,"
                    " manifest_digest, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        receipt_id,
                        doc["submission_id"],
                        doc["rule_id"],
                        doc["batch"],
                        doc["manifest_digest"],
                        payload_json,
                    ),
                )
                for entry in doc["expected"]:
                    conn.execute(
                        "INSERT INTO expected(receipt_id, orig_no, path_key, orig_index,"
                        " category, grams, piece_id, content_hash)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            receipt_id,
                            entry["orig_no"],
                            entry["path_key"],
                            entry["orig_index"],
                            entry["category"],
                            entry["grams"],
                            entry["piece_id"],
                            entry["content_digest"],
                        ),
                    )
                self._event(
                    conn, receipt_id, "declared",
                    {"submission_id": doc["submission_id"],
                     "manifest_digest": doc["manifest_digest"],
                     "expected_count": len(doc["expected"])},
                )
                return {
                    "receipt_id": receipt_id,
                    "submission_id": doc["submission_id"],
                    "manifest_digest": doc["manifest_digest"],
                    "replayed": False,
                    "status": self._status(conn, receipt_id),
                }
        finally:
            close(conn)

    # --------------------------------------------------------------- posting
    def post_piece(self, payload: dict) -> dict:
        entry = model.validate_piece_payload(payload)
        receipt_id = entry["receipt_id"]
        orig_no = entry["orig_no"]
        path_key = entry["path_key"]
        piece_json = canonical(payload)

        conn = self._conn()
        try:
            with transaction(conn):
                stored = conn.execute(
                    "SELECT piece_json FROM received"
                    " WHERE receipt_id = ? AND orig_no = ? AND path_key = ?",
                    (receipt_id, orig_no, path_key),
                ).fetchone()
                if stored is not None:
                    if stored["piece_json"] != piece_json:
                        raise ChengqiaoError(
                            "text-conflict",
                            "this occurrence was already received with different text",
                            {"receipt_id": receipt_id, "orig_no": orig_no,
                             "path": path_key},
                        )
                    return {
                        "receipt_id": receipt_id,
                        "orig_no": orig_no,
                        "path": path_key,
                        "piece_id": entry["piece_id"],
                        "replayed": True,
                        "status": self._status(conn, receipt_id),
                    }

                declared = conn.execute(
                    "SELECT submission_id FROM declared WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone()
                if declared is None:
                    raise ChengqiaoError(
                        "not-declared",
                        "submission receipt has not been declared at the account end",
                        {"receipt_id": receipt_id},
                    )

                if entry["submission_id"] != declared["submission_id"]:
                    raise ChengqiaoError(
                        "misbound",
                        "piece is bound to a different submission than its receipt",
                        {
                            "receipt_id": receipt_id,
                            "piece_submission_id": entry["submission_id"],
                            "declared_submission_id": declared["submission_id"],
                        },
                    )

                expected = conn.execute(
                    "SELECT piece_id, content_hash FROM expected"
                    " WHERE receipt_id = ? AND orig_no = ? AND path_key = ?",
                    (receipt_id, orig_no, path_key),
                ).fetchone()
                if expected is None:
                    raise ChengqiaoError(
                        "text-conflict",
                        "path is not part of the declared submission",
                        {"receipt_id": receipt_id, "orig_no": orig_no, "path": path_key},
                    )
                if (
                    expected["piece_id"] != entry["piece_id"]
                    or expected["content_hash"] != entry["content_digest"]
                ):
                    raise ChengqiaoError(
                        "text-conflict",
                        "piece text differs from the declared expectation",
                        {"receipt_id": receipt_id, "orig_no": orig_no, "path": path_key},
                    )

                conn.execute(
                    "INSERT INTO received(receipt_id, orig_no, path_key, piece_id,"
                    " content_hash, piece_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        receipt_id,
                        orig_no,
                        path_key,
                        entry["piece_id"],
                        entry["content_digest"],
                        piece_json,
                    ),
                )
                self._event(
                    conn, receipt_id, "piece-posted",
                    {"orig_no": orig_no, "path": path_key,
                     "piece_id": entry["piece_id"]},
                )
                return {
                    "receipt_id": receipt_id,
                    "orig_no": orig_no,
                    "path": path_key,
                    "piece_id": entry["piece_id"],
                    "replayed": False,
                    "status": self._status(conn, receipt_id),
                }
        finally:
            close(conn)

    # ---------------------------------------------------------------- status
    def _status(self, conn, receipt_id: str) -> dict:
        declared = conn.execute(
            "SELECT submission_id FROM declared WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()
        seal = conn.execute(
            "SELECT result_id FROM seals WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()
        if declared is None:
            return {"state": "unknown", "declared": False, "sealed": False}
        expected = conn.execute(
            "SELECT COUNT(*) AS n FROM expected WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()["n"]
        got = conn.execute(
            "SELECT COUNT(*) AS n FROM received WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()["n"]
        if seal is not None:
            state = "sealed"
        elif got == expected:
            state = "complete"
        else:
            state = "pending"
        return {
            "state": state,
            "declared": True,
            "sealed": seal is not None,
            "result_id": seal["result_id"] if seal else None,
            "expected_count": expected,
            "received_count": got,
            "missing_count": max(expected - got, 0),
        }

    def _missing(self, conn, receipt_id: str) -> list[dict]:
        rows = conn.execute(
            "SELECT e.orig_no, e.path_key, e.orig_index, e.category, e.grams"
            " FROM expected e"
            " LEFT JOIN received r ON r.receipt_id = e.receipt_id"
            " AND r.orig_no = e.orig_no AND r.path_key = e.path_key"
            " WHERE e.receipt_id = ? AND r.receipt_id IS NULL",
            (receipt_id,),
        ).fetchall()
        rows = sorted(
            rows, key=lambda row: (row["orig_index"], _path_sort_key(row["path_key"]))
        )
        return [
            {
                "orig_no": row["orig_no"],
                "path": list(row["path_key"]),
                "category": row["category"],
                "grams": row["grams"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ seal
    def seal(self, receipt_id: str) -> dict:
        conn = self._conn()
        try:
            with transaction(conn):
                declared = conn.execute(
                    "SELECT * FROM declared WHERE receipt_id = ?", (receipt_id,)
                ).fetchone()
                if declared is None:
                    raise ChengqiaoError(
                        "unknown-receipt",
                        f"receipt {receipt_id!r} was never declared at the account end",
                        {"receipt_id": receipt_id},
                    )
                existing = conn.execute(
                    "SELECT result_json FROM seals WHERE receipt_id = ?", (receipt_id,)
                ).fetchone()
                if existing is not None:
                    return {
                        "result": _load(existing["result_json"]),
                        "replayed": True,
                        "status": self._status(conn, receipt_id),
                    }

                missing = self._missing(conn, receipt_id)
                if missing:
                    raise ChengqiaoError(
                        "pending",
                        "not every expected endpoint occurrence has been received",
                        {"receipt_id": receipt_id, "missing": missing},
                    )

                expected = conn.execute(
                    "SELECT * FROM expected WHERE receipt_id = ?", (receipt_id,)
                ).fetchall()
                expected = sorted(
                    expected,
                    key=lambda row: (row["orig_index"], _path_sort_key(row["path_key"])),
                )
                detail = []
                totals: dict[str, int] = {}
                for row in expected:
                    detail.append(
                        {
                            "orig_no": row["orig_no"],
                            "path": list(row["path_key"]),
                            "category": row["category"],
                            "grams": row["grams"],
                            "piece_id": row["piece_id"],
                            "content_digest": row["content_hash"],
                        }
                    )
                    totals[row["category"]] = totals.get(row["category"], 0) + row["grams"]

                result = {
                    "result_id": f"SEAL-{receipt_id}",
                    "receipt_id": receipt_id,
                    "submission_id": declared["submission_id"],
                    "rule_id": declared["rule_id"],
                    "batch": declared["batch"],
                    "manifest_digest": declared["manifest_digest"],
                    "receipt": _load(declared["payload_json"]),
                    "detail": detail,
                    "totals": [
                        {"category": name, "grams": totals[name]}
                        for name in sorted(totals)
                    ],
                    "total_grams": sum(totals.values()),
                    "piece_count": len(detail),
                }
                conn.execute(
                    "INSERT INTO seals(result_id, receipt_id, manifest_digest, result_json)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        result["result_id"], receipt_id, declared["manifest_digest"],
                        canonical(result),
                    ),
                )
                self._event(
                    conn, receipt_id, "sealed",
                    {"result_id": result["result_id"], "piece_count": len(detail)},
                )
                return {
                    "result": result,
                    "replayed": False,
                    "status": self._status(conn, receipt_id),
                }
        finally:
            close(conn)

    # ---------------------------------------------------------------- lookup
    RESULT_PREFIX = "SEAL-"

    def lookup(self, ref: str) -> dict:
        """Look up by receipt id or result id; unknown vs pending is explicit."""
        conn = self._conn()
        try:
            seal = conn.execute(
                "SELECT result_json FROM seals WHERE result_id = ?"
                " OR receipt_id = ?",
                (ref, ref),
            ).fetchone()
            if seal is not None:
                result = _load(seal["result_json"])
                return {
                    "state": "sealed",
                    "ref": ref,
                    "receipt_id": result["receipt_id"],
                    "result_id": result["result_id"],
                    "result": result,
                }
            # A result id of a declared-but-unsealed receipt is still pending.
            receipt_id = ref
            if ref.startswith(self.RESULT_PREFIX):
                receipt_id = ref[len(self.RESULT_PREFIX):]
            declared = conn.execute(
                "SELECT * FROM declared WHERE receipt_id = ?", (receipt_id,)
            ).fetchone()
            if declared is None:
                return {"state": "unknown", "ref": ref}
            return {
                "state": "pending",
                "ref": ref,
                "receipt_id": receipt_id,
                "submission_id": declared["submission_id"],
                "manifest_digest": declared["manifest_digest"],
                "missing": self._missing(conn, receipt_id),
                "status": self._status(conn, receipt_id),
            }
        finally:
            close(conn)

    def result(self, result_id: str) -> dict:
        found = self.lookup(result_id)
        if found["state"] != "sealed":
            raise ChengqiaoError(
                "unknown-result",
                f"result {result_id!r} is not sealed",
                {"result_id": result_id, "state": found["state"]},
            )
        return found["result"]
