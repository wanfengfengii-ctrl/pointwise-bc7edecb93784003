"""Processing store: frozen rules, accepted orders, traversal and submission.

One SQLite file.  Every operation opens its own connection and runs inside a
single explicit transaction, so a crash can only leave the store at a state
that was committed on purpose.  Processing writes nothing to the account
store: it only produces receipt payloads (plain JSON) for the night shift to
hand over.

Traversal model
---------------
A weighing arrives at a start node with grams.  A *terminal* node is a
counterpart: arrival there records exactly one endpoint piece.  A *branch*
node has a non-negative limit and the two named edges ``L``/``R``; crossing
``L`` sends ``min(incoming, limit)`` and crossing ``R`` sends the remainder.

A ready item is one arrival at one node, identified by
``(receipt_id, orig_no, full path)`` -- the full sequence of named edges
walked from the weighing's origin.  Advancing a ready item is one action:

* at a branch node it emits the two child arrivals (left gets the lesser of
  incoming and the limit, right gets the remainder), and
* at a terminal node it records that endpoint piece.

Two different routes that reach the same node stay separate ready items
because their full paths differ, so a merge never combines amounts.  Zero
grams are walked like any other amount.  Re-advancing an already advanced
item returns its stored original outcome.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import model, rules as rules_mod
from .canon import canonical, digest, is_nonempty_str, is_nonneg_int
from .errors import ChengqiaoError
from .sqlite_util import close, connect, transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rules (
    rule_id   TEXT PRIMARY KEY,
    digest    TEXT NOT NULL,
    spec_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    receipt_id  TEXT PRIMARY KEY,
    batch       TEXT NOT NULL,
    rule_id     TEXT NOT NULL REFERENCES rules(rule_id),
    params_json TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    accepted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS weighings (
    receipt_id TEXT NOT NULL REFERENCES orders(receipt_id),
    orig_no    TEXT NOT NULL,
    start_node TEXT NOT NULL,
    grams      INTEGER NOT NULL CHECK (grams >= 0),
    position   INTEGER NOT NULL,
    PRIMARY KEY (receipt_id, orig_no),
    UNIQUE (receipt_id, position)
);
CREATE TABLE IF NOT EXISTS ready (
    receipt_id TEXT NOT NULL,
    orig_no    TEXT NOT NULL,
    path_key   TEXT NOT NULL,
    node       TEXT NOT NULL,
    grams      INTEGER NOT NULL CHECK (grams >= 0),
    done       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (receipt_id, orig_no, path_key)
);
CREATE TABLE IF NOT EXISTS advances (
    receipt_id  TEXT NOT NULL,
    orig_no     TEXT NOT NULL,
    path_key    TEXT NOT NULL,
    result_json TEXT NOT NULL,
    advanced_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (receipt_id, orig_no, path_key)
);
CREATE TABLE IF NOT EXISTS pieces (
    receipt_id   TEXT NOT NULL,
    orig_no      TEXT NOT NULL,
    orig_index   INTEGER NOT NULL,
    path_key     TEXT NOT NULL,
    category     TEXT NOT NULL,
    grams        INTEGER NOT NULL CHECK (grams >= 0),
    piece_id     TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    piece_json   TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (receipt_id, orig_no, path_key),
    UNIQUE (receipt_id, piece_id)
);
CREATE TABLE IF NOT EXISTS submissions (
    receipt_id    TEXT PRIMARY KEY REFERENCES orders(receipt_id),
    submission_id TEXT NOT NULL UNIQUE,
    manifest_hash TEXT NOT NULL,
    piece_count   INTEGER NOT NULL,
    payload_json  TEXT NOT NULL,
    submitted_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
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


def piece_id_for(receipt_id: str, orig_no: str, path_key: str) -> str:
    return f"{receipt_id}::{orig_no}::{path_key or '$'}"


def _path_key(path) -> str:
    if isinstance(path, str):
        edges = list(path)
    elif isinstance(path, (list, tuple)):
        edges = list(path)
    else:
        raise ChengqiaoError("invalid-path", "path must be text or a list of edges")
    for edge in edges:
        if edge not in rules_mod.EDGES:
            raise ChengqiaoError("invalid-path", "path edges must be 'L' or 'R'")
    return "".join(edges)


class ProcessStore:
    """Frozen named rules plus processing-end state for one working day."""

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

    # ---------------------------------------------------------------- rules
    def freeze_rule(self, raw) -> dict:
        rule = rules_mod.parse_rule_spec(raw)
        rule_json = canonical(rule)
        rule_hash = rules_mod.rule_digest(rule)
        conn = self._conn()
        try:
            with transaction(conn):
                existing = conn.execute(
                    "SELECT digest FROM rules WHERE rule_id = ?", (rule["rule_id"],)
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO rules(rule_id, digest, spec_json) VALUES (?, ?, ?)",
                        (rule["rule_id"], rule_hash, rule_json),
                    )
                    self._event(conn, None, "rule-frozen", {"rule_id": rule["rule_id"]})
                    created = True
                elif existing["digest"] == rule_hash:
                    created = False
                else:
                    raise ChengqiaoError(
                        "rule-frozen",
                        "rule id is already frozen with different content",
                        {"rule_id": rule["rule_id"]},
                    )
            return {
                "rule_id": rule["rule_id"],
                "rule_digest": rule_hash,
                "created": created,
                "node_count": len(rule["nodes"]),
            }
        finally:
            close(conn)

    def get_rule(self, rule_id: str) -> dict:
        conn = self._conn()
        try:
            return self._load_rule(conn, rule_id)
        finally:
            close(conn)

    def _load_rule(self, conn, rule_id: str) -> dict:
        row = conn.execute(
            "SELECT spec_json FROM rules WHERE rule_id = ?", (rule_id,)
        ).fetchone()
        if row is None:
            raise ChengqiaoError(
                "unknown-rule", f"rule {rule_id!r} is not frozen", {"rule_id": rule_id}
            )
        return _load(row["spec_json"])

    # --------------------------------------------------------------- orders
    def accept_order(self, batch, rule_id, weighings) -> dict:
        if not is_nonempty_str(batch):
            raise ChengqiaoError("invalid-order", "batch must be a non-empty string")
        if not is_nonempty_str(rule_id):
            raise ChengqiaoError("invalid-order", "rule_id must be a non-empty string")
        if not isinstance(weighings, list) or not weighings:
            raise ChengqiaoError("invalid-order", "weighings must be a non-empty list")

        normalized = []
        seen = set()
        for index, raw in enumerate(weighings):
            if not isinstance(raw, dict):
                raise ChengqiaoError(
                    "invalid-order", f"weighings[{index}] must be an object"
                )
            if set(raw) != {"orig_no", "start", "grams"}:
                raise ChengqiaoError(
                    "invalid-order",
                    f"weighings[{index}] must have exactly orig_no, start and grams",
                )
            orig_no = raw["orig_no"]
            start = raw["start"]
            grams = raw["grams"]
            if not is_nonempty_str(orig_no):
                raise ChengqiaoError(
                    "invalid-order", f"weighings[{index}] needs a non-empty orig_no"
                )
            if not is_nonempty_str(start):
                raise ChengqiaoError(
                    "invalid-order", f"weighings[{index}] needs a start node"
                )
            if not is_nonneg_int(grams):
                raise ChengqiaoError(
                    "invalid-order",
                    f"weighings[{index}] grams must be a non-negative integer",
                )
            if orig_no in seen:
                raise ChengqiaoError(
                    "duplicate-orig",
                    f"orig_no {orig_no!r} repeats in one batch",
                    {"orig_no": orig_no},
                )
            seen.add(orig_no)
            normalized.append({"orig_no": orig_no, "start": start, "grams": grams})

        conn = self._conn()
        try:
            rule = self._load_rule(conn, rule_id)
            for index, item in enumerate(normalized):
                if item["start"] not in rule["nodes"]:
                    raise ChengqiaoError(
                        "bad-reference",
                        f"weighings[{index}] start node is not in the rule",
                        {"orig_no": item["orig_no"], "start": item["start"]},
                    )

            params = {"batch": batch, "rule_id": rule_id, "weighings": normalized}
            params_hash = digest(params)

            with transaction(conn):
                rows = conn.execute(
                    "SELECT receipt_id, params_hash FROM orders WHERE batch = ?",
                    (batch,),
                ).fetchall()
                for row in rows:
                    if row["params_hash"] == params_hash:
                        return self._accept_reply(conn, row["receipt_id"], reissued=True)
                if rows:
                    raise ChengqiaoError(
                        "param-conflict",
                        "batch was already accepted with different parameters",
                        {"batch": batch},
                    )

                receipt_id = self._next_receipt_id(conn, batch)
                conn.execute(
                    "INSERT INTO orders(receipt_id, batch, rule_id, params_json,"
                    " params_hash) VALUES (?, ?, ?, ?, ?)",
                    (receipt_id, batch, rule_id, canonical(params), params_hash),
                )
                for position, item in enumerate(normalized):
                    conn.execute(
                        "INSERT INTO weighings(receipt_id, orig_no, start_node, grams,"
                        " position) VALUES (?, ?, ?, ?, ?)",
                        (receipt_id, item["orig_no"], item["start"], item["grams"], position),
                    )
                    node = item["start"]
                    if rules_mod.is_terminal(rule, node):
                        self._record_terminal(
                            conn,
                            receipt_id=receipt_id,
                            orig_no=item["orig_no"],
                            orig_index=position,
                            path_key="",
                            rule=rule,
                            node=node,
                            grams=item["grams"],
                        )
                    else:
                        conn.execute(
                            "INSERT INTO ready(receipt_id, orig_no, path_key, node, grams)"
                            " VALUES (?, ?, '', ?, ?)",
                            (receipt_id, item["orig_no"], node, item["grams"]),
                        )
                self._event(
                    conn, receipt_id, "order-accepted",
                    {"batch": batch, "rule_id": rule_id, "params_hash": params_hash},
                )
                return self._accept_reply(conn, receipt_id, reissued=False)
        finally:
            close(conn)

    def _accept_reply(self, conn, receipt_id: str, reissued: bool) -> dict:
        return {
            "receipt_id": receipt_id,
            "reissued": reissued,
            "order": self._order_payload(conn, receipt_id),
            "status": self._status(conn, receipt_id),
        }

    def _next_receipt_id(self, conn, batch: str) -> str:
        row = conn.execute("SELECT value FROM meta WHERE key = 'receipt_seq'").fetchone()
        seq = int(row["value"]) if row else 0
        while True:
            seq += 1
            candidate = f"{batch}-{seq:04d}"
            if conn.execute(
                "SELECT 1 FROM orders WHERE receipt_id = ?", (candidate,)
            ).fetchone() is None:
                break
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('receipt_seq', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(seq),),
        )
        return candidate

    def _require_order(self, conn, receipt_id: str):
        row = conn.execute(
            "SELECT * FROM orders WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()
        if row is None:
            raise ChengqiaoError(
                "unknown-receipt",
                f"receipt {receipt_id!r} is not at the processing end",
                {"receipt_id": receipt_id},
            )
        return row

    def _order_payload(self, conn, receipt_id: str) -> dict:
        order = self._require_order(conn, receipt_id)
        params = _load(order["params_json"])
        return {
            "receipt_id": receipt_id,
            "batch": order["batch"],
            "rule_id": order["rule_id"],
            "weighings": params["weighings"],
        }

    def _status(self, conn, receipt_id: str) -> dict:
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM ready WHERE receipt_id = ? AND done = 0",
            (receipt_id,),
        ).fetchone()["n"]
        piece_count = conn.execute(
            "SELECT COUNT(*) AS n FROM pieces WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()["n"]
        submission = conn.execute(
            "SELECT submission_id FROM submissions WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()
        return {
            "finished": pending == 0,
            "pending_ready": pending,
            "piece_count": piece_count,
            "submitted": submission is not None,
            "submission_id": submission["submission_id"] if submission else None,
        }

    def order(self, receipt_id: str) -> dict:
        conn = self._conn()
        try:
            payload = self._order_payload(conn, receipt_id)
            payload["status"] = self._status(conn, receipt_id)
            return payload
        finally:
            close(conn)

    # ------------------------------------------------------------ traversal
    def ready_items(self, receipt_id: str) -> dict:
        conn = self._conn()
        try:
            self._require_order(conn, receipt_id)
            return {
                "receipt_id": receipt_id,
                "ready": self._ready_list(conn, receipt_id),
                "status": self._status(conn, receipt_id),
            }
        finally:
            close(conn)

    def _ready_list(self, conn, receipt_id: str) -> list[dict]:
        rows = conn.execute(
            "SELECT orig_no, path_key, node, grams FROM ready WHERE receipt_id = ?"
            " AND done = 0 ORDER BY orig_no, path_key",
            (receipt_id,),
        ).fetchall()
        return [
            {
                "orig_no": row["orig_no"],
                "path": list(row["path_key"]),
                "node": row["node"],
                "grams": row["grams"],
            }
            for row in rows
        ]

    def advance(self, receipt_id: str, orig_no: str, path) -> dict:
        """Advance the ready item at ``(receipt_id, orig_no, full path)``.

        At a branch node the item splits into its two named children; at a
        terminal node the endpoint piece is recorded.  Repeating the call
        returns the stored original outcome instead of doing the work again.
        """
        path_key = _path_key(path)
        conn = self._conn()
        try:
            order = self._require_order(conn, receipt_id)
            rule = self._load_rule(conn, order["rule_id"])
            with transaction(conn):
                stored = conn.execute(
                    "SELECT result_json FROM advances WHERE receipt_id = ?"
                    " AND orig_no = ? AND path_key = ?",
                    (receipt_id, orig_no, path_key),
                ).fetchone()
                if stored is not None:
                    return {"result": _load(stored["result_json"]), "replayed": True}

                row = conn.execute(
                    "SELECT node, grams, done FROM ready WHERE receipt_id = ?"
                    " AND orig_no = ? AND path_key = ?",
                    (receipt_id, orig_no, path_key),
                ).fetchone()
                if row is None:
                    raise ChengqiaoError(
                        "not-ready",
                        "no ready item matches that original and full path",
                        {"receipt_id": receipt_id, "orig_no": orig_no,
                         "path": path_key},
                    )
                if row["done"]:
                    raise ChengqiaoError(
                        "inconsistent-store",
                        "ready item is marked done but has no stored outcome",
                        {"receipt_id": receipt_id, "orig_no": orig_no,
                         "path": path_key},
                    )

                node = row["node"]
                grams = row["grams"]
                orig_index = conn.execute(
                    "SELECT position FROM weighings WHERE receipt_id = ? AND orig_no = ?",
                    (receipt_id, orig_no),
                ).fetchone()["position"]

                if rules_mod.is_terminal(rule, node):
                    piece = self._record_terminal(
                        conn,
                        receipt_id=receipt_id,
                        orig_no=orig_no,
                        orig_index=orig_index,
                        path_key=path_key,
                        rule=rule,
                        node=node,
                        grams=grams,
                    )
                    result = {
                        "receipt_id": receipt_id,
                        "orig_no": orig_no,
                        "path": list(path_key),
                        "node": node,
                        "grams": grams,
                        "kind": "terminal",
                        "piece": piece,
                    }
                else:
                    spec = rule["nodes"][node]
                    left_amount = min(grams, spec["limit"])
                    right_amount = grams - left_amount
                    children = []
                    for edge, target, amount in (
                        ("L", spec["left"], left_amount),
                        ("R", spec["right"], right_amount),
                    ):
                        child_path = path_key + edge
                        child = {
                            "orig_no": orig_no,
                            "path": list(child_path),
                            "node": target,
                            "grams": amount,
                        }
                        if rules_mod.is_terminal(rule, target):
                            child["kind"] = "terminal"
                            child["piece"] = self._record_terminal(
                                conn,
                                receipt_id=receipt_id,
                                orig_no=orig_no,
                                orig_index=orig_index,
                                path_key=child_path,
                                rule=rule,
                                node=target,
                                grams=amount,
                            )
                        else:
                            child["kind"] = "branch"
                            conn.execute(
                                "INSERT INTO ready(receipt_id, orig_no, path_key, node,"
                                " grams) VALUES (?, ?, ?, ?, ?)",
                                (receipt_id, orig_no, child_path, target, amount),
                            )
                        children.append(child)
                    result = {
                        "receipt_id": receipt_id,
                        "orig_no": orig_no,
                        "path": list(path_key),
                        "node": node,
                        "grams": grams,
                        "kind": "branch",
                        "limit": spec["limit"],
                        "children": children,
                    }

                conn.execute(
                    "UPDATE ready SET done = 1 WHERE receipt_id = ? AND orig_no = ?"
                    " AND path_key = ?",
                    (receipt_id, orig_no, path_key),
                )
                conn.execute(
                    "INSERT INTO advances(receipt_id, orig_no, path_key, result_json)"
                    " VALUES (?, ?, ?, ?)",
                    (receipt_id, orig_no, path_key, canonical(result)),
                )
                self._event(
                    conn, receipt_id, "advance",
                    {"orig_no": orig_no, "path": path_key, "kind": result["kind"]},
                )
                return {"result": result, "replayed": False}
        finally:
            close(conn)

    def _record_terminal(
        self, conn, *, receipt_id, orig_no, orig_index, path_key, rule, node, grams
    ) -> dict:
        piece_id = piece_id_for(receipt_id, orig_no, path_key)
        entry = {
            "piece_id": piece_id,
            "receipt_id": receipt_id,
            "rule_id": rule["rule_id"],
            "orig_no": orig_no,
            "orig_index": orig_index,
            "path": list(path_key),
            "category": rules_mod.category_of(rule, node),
            "grams": grams,
        }
        entry["content_digest"] = model.piece_digest(entry)
        conn.execute(
            "INSERT INTO pieces(receipt_id, orig_no, orig_index, path_key, category,"
            " grams, piece_id, content_hash, piece_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                receipt_id, orig_no, orig_index, path_key, entry["category"], grams,
                piece_id, entry["content_digest"], canonical(entry),
            ),
        )
        return entry

    # ------------------------------------------------------------ submission
    def submit(self, receipt_id: str) -> dict:
        """Assemble the processing-end submission receipt for one order."""
        conn = self._conn()
        try:
            order = self._require_order(conn, receipt_id)
            with transaction(conn):
                pending = self._ready_list(conn, receipt_id)
                if pending:
                    raise ChengqiaoError(
                        "not-complete",
                        "processing traversal still has ready items",
                        {"receipt_id": receipt_id, "ready": pending},
                    )
                existing = conn.execute(
                    "SELECT payload_json FROM submissions WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone()
                if existing is not None:
                    return {"submission": _load(existing["payload_json"]), "replayed": True}

                expected = self._pieces(conn, receipt_id)
                if not expected:
                    raise ChengqiaoError(
                        "not-complete",
                        "processing traversal produced no endpoint pieces",
                        {"receipt_id": receipt_id},
                    )
                submission_id = f"SUB-{receipt_id}"
                manifest_hash = model.manifest_digest(
                    receipt_id, order["batch"], order["rule_id"], expected
                )
                payload = model.make_submission_payload(
                    receipt_id=receipt_id,
                    submission_id=submission_id,
                    rule_id=order["rule_id"],
                    batch=order["batch"],
                    manifest_hash=manifest_hash,
                    expected=expected,
                )
                conn.execute(
                    "INSERT INTO submissions(receipt_id, submission_id, manifest_hash,"
                    " piece_count, payload_json) VALUES (?, ?, ?, ?, ?)",
                    (receipt_id, submission_id, manifest_hash, len(expected),
                     canonical(payload)),
                )
                self._event(
                    conn, receipt_id, "submitted",
                    {"submission_id": submission_id, "manifest_hash": manifest_hash,
                     "piece_count": len(expected)},
                )
                return {"submission": payload, "replayed": False}
        finally:
            close(conn)

    def _pieces(self, conn, receipt_id: str) -> list[dict]:
        rows = conn.execute(
            "SELECT piece_json FROM pieces WHERE receipt_id = ?"
            " ORDER BY orig_index, path_key",
            (receipt_id,),
        ).fetchall()
        return [_load(row["piece_json"]) for row in rows]

    def pieces(self, receipt_id: str) -> dict:
        conn = self._conn()
        try:
            self._require_order(conn, receipt_id)
            return {
                "receipt_id": receipt_id,
                "pieces": self._pieces(conn, receipt_id),
                "status": self._status(conn, receipt_id),
            }
        finally:
            close(conn)
