"""Shared payload shapes for endpoint pieces and submission manifests.

The two SQLite stores stay independent: the account end re-derives every
digest from the payload text it receives instead of reading the processing
store.

Identity of one endpoint occurrence is the triple
``(receipt_id, orig_no, path)`` where ``path`` is the full sequence of named
edges walked from the weighing's origin node.  Because each order has unique
original weighing numbers, that triple is unique even when two different
routes reach the same terminal node (a merge stays split into separate
occurrences).
"""

from __future__ import annotations

from .canon import digest, is_int, is_nonempty_str, is_nonneg_int
from .errors import ChengqiaoError

EDGE_NAMES = ("L", "R")

SUBMISSION_KIND = "chengqiao.submission.v1"
PIECE_KIND = "chengqiao.piece.v1"

# Fields that identify the content of one endpoint piece.
PIECE_FIELDS = (
    "piece_id",
    "receipt_id",
    "rule_id",
    "orig_no",
    "orig_index",
    "path",
    "category",
    "grams",
)

# Fields that make up the canonical submission manifest.
MANIFEST_FIELDS = PIECE_FIELDS + ("content_digest",)

_PIECE_ALLOWED = set(PIECE_FIELDS) | {"content_digest", "kind", "submission_id"}
_SUBMISSION_ALLOWED = {
    "kind",
    "receipt_id",
    "submission_id",
    "rule_id",
    "batch",
    "manifest_digest",
    "expected",
}


def normalize_path(value) -> tuple[list[str], str]:
    """Accept ``"LR"`` or ``["L", "R"]``; return edges plus storage key."""
    if isinstance(value, str):
        edges = list(value)
    elif isinstance(value, (list, tuple)):
        edges = list(value)
    else:
        raise ChengqiaoError("invalid-path", "path must be text or a list of edges")
    for edge in edges:
        if edge not in EDGE_NAMES:
            raise ChengqiaoError(
                "invalid-path", "path edges must be 'L' or 'R'", {"path": value}
            )
    return edges, "".join(edges)


def _project(entry: dict, fields: tuple[str, ...]) -> dict:
    out = {}
    for field in fields:
        value = entry[field]
        out[field] = list(value) if field == "path" else value
    return out


def piece_content(entry: dict) -> dict:
    """Canonical content that identifies one endpoint occurrence."""
    return _project(entry, PIECE_FIELDS)


def piece_digest(entry: dict) -> str:
    return digest(piece_content(entry))


def build_manifest(receipt_id: str, batch: str, rule_id: str, expected: list[dict]) -> dict:
    return {
        "receipt_id": receipt_id,
        "batch": batch,
        "rule_id": rule_id,
        "expected": [_project(entry, MANIFEST_FIELDS) for entry in expected],
    }


def manifest_digest(receipt_id: str, batch: str, rule_id: str, expected: list[dict]) -> str:
    return digest(build_manifest(receipt_id, batch, rule_id, expected))


def make_submission_payload(
    *,
    receipt_id: str,
    submission_id: str,
    rule_id: str,
    batch: str,
    manifest_hash: str,
    expected: list[dict],
) -> dict:
    return {
        "kind": SUBMISSION_KIND,
        "receipt_id": receipt_id,
        "submission_id": submission_id,
        "rule_id": rule_id,
        "batch": batch,
        "manifest_digest": manifest_hash,
        "expected": [_project(entry, MANIFEST_FIELDS) for entry in expected],
    }


def make_piece_payload(entry: dict, submission_id: str) -> dict:
    payload = _project(entry, PIECE_FIELDS)
    payload["content_digest"] = entry["content_digest"]
    payload["submission_id"] = submission_id
    payload["kind"] = PIECE_KIND
    return payload


def validate_piece_payload(raw: dict) -> dict:
    """Validate one posted endpoint original using only its own text."""
    if not isinstance(raw, dict):
        raise ChengqiaoError("invalid-piece", "piece payload must be an object")
    unknown = set(raw) - _PIECE_ALLOWED
    if unknown:
        raise ChengqiaoError(
            "invalid-piece", f"unknown piece fields: {sorted(unknown)}"
        )
    if "kind" in raw and raw["kind"] != PIECE_KIND:
        raise ChengqiaoError("invalid-piece", "piece payload has the wrong kind")

    missing = [field for field in PIECE_FIELDS if field not in raw]
    if missing:
        raise ChengqiaoError(
            "invalid-piece", f"piece payload is missing {missing[0]}"
        )
    content_digest = raw.get("content_digest")
    submission_id = raw.get("submission_id")
    if not is_nonempty_str(content_digest):
        raise ChengqiaoError("invalid-piece", "piece payload needs content_digest")
    if not is_nonempty_str(submission_id):
        raise ChengqiaoError("invalid-piece", "piece payload needs submission_id")

    if not is_nonempty_str(raw["piece_id"]):
        raise ChengqiaoError("invalid-piece", "piece_id must be non-empty")
    if not is_nonempty_str(raw["receipt_id"]):
        raise ChengqiaoError("invalid-piece", "receipt_id must be non-empty")
    if not is_nonempty_str(raw["rule_id"]):
        raise ChengqiaoError("invalid-piece", "rule_id must be non-empty")
    if not is_nonempty_str(raw["orig_no"]):
        raise ChengqiaoError("invalid-piece", "orig_no must be non-empty")
    if not is_int(raw["orig_index"]) or raw["orig_index"] < 0:
        raise ChengqiaoError("invalid-piece", "orig_index must be a non-negative integer")
    if not is_nonempty_str(raw["category"]):
        raise ChengqiaoError("invalid-piece", "category must be non-empty")
    if not is_nonneg_int(raw["grams"]):
        raise ChengqiaoError("invalid-piece", "grams must be a non-negative integer")

    edges, path_key = normalize_path(raw["path"])
    entry = {field: raw[field] for field in PIECE_FIELDS}
    entry["path"] = edges
    if piece_digest(entry) != content_digest:
        raise ChengqiaoError(
            "invalid-piece",
            "piece content digest does not match its text",
            {"piece_id": entry["piece_id"]},
        )
    entry["content_digest"] = content_digest
    entry["submission_id"] = submission_id
    entry["path_key"] = path_key
    return entry


def validate_manifest_payload(payload: dict) -> dict:
    """Validate a submission receipt end-to-end using only its own text.

    Returns a normalized structure.  Any inconsistency means the account end
    is not looking at an original submission, so it is rejected.
    """
    if not isinstance(payload, dict):
        raise ChengqiaoError("invalid-submission", "submission receipt must be an object")
    unknown = set(payload) - _SUBMISSION_ALLOWED
    if unknown:
        raise ChengqiaoError(
            "invalid-submission", f"unknown submission fields: {sorted(unknown)}"
        )
    if "kind" in payload and payload["kind"] != SUBMISSION_KIND:
        raise ChengqiaoError("invalid-submission", "submission receipt has the wrong kind")

    receipt_id = payload.get("receipt_id")
    submission_id = payload.get("submission_id")
    rule_id = payload.get("rule_id")
    batch = payload.get("batch")
    given_digest = payload.get("manifest_digest")
    expected = payload.get("expected")

    for name, value in (
        ("receipt_id", receipt_id),
        ("submission_id", submission_id),
        ("rule_id", rule_id),
        ("batch", batch),
        ("manifest_digest", given_digest),
    ):
        if not is_nonempty_str(value):
            raise ChengqiaoError(
                "invalid-submission", f"submission receipt needs non-empty {name}"
            )
    if not isinstance(expected, list) or not expected:
        raise ChengqiaoError(
            "invalid-submission", "submission receipt needs a non-empty expected list"
        )

    normalized: list[dict] = []
    piece_ids: set[str] = set()
    slots: set[tuple[str, str]] = set()
    for index, raw in enumerate(expected):
        if not isinstance(raw, dict):
            raise ChengqiaoError(
                "invalid-submission", f"expected[{index}] must be an object"
            )
        unknown = set(raw) - set(MANIFEST_FIELDS)
        if unknown:
            raise ChengqiaoError(
                "invalid-submission",
                f"expected[{index}] has unknown fields: {sorted(unknown)}",
            )
        try:
            entry = {field: raw[field] for field in MANIFEST_FIELDS}
        except KeyError as exc:
            raise ChengqiaoError(
                "invalid-submission", f"expected[{index}] is missing {exc.args[0]}"
            ) from None

        piece_id = entry["piece_id"]
        if not is_nonempty_str(piece_id):
            raise ChengqiaoError(
                "invalid-submission", f"expected[{index}] needs a non-empty piece_id"
            )
        if not is_nonempty_str(entry["orig_no"]):
            raise ChengqiaoError(
                "invalid-submission", f"expected[{index}] needs a non-empty orig_no"
            )
        if not is_int(entry["orig_index"]) or entry["orig_index"] < 0:
            raise ChengqiaoError(
                "invalid-submission",
                f"expected[{index}] orig_index must be a non-negative integer",
            )
        if not is_nonempty_str(entry["category"]):
            raise ChengqiaoError(
                "invalid-submission", f"expected[{index}] needs a category"
            )
        if not is_nonneg_int(entry["grams"]):
            raise ChengqiaoError(
                "invalid-submission", f"expected[{index}] grams must be a non-negative integer"
            )
        if entry["receipt_id"] != receipt_id or entry["rule_id"] != rule_id:
            raise ChengqiaoError(
                "invalid-submission",
                f"expected[{index}] is bound to a different receipt or rule",
            )
        if not is_nonempty_str(entry["content_digest"]):
            raise ChengqiaoError(
                "invalid-submission", f"expected[{index}] needs a content_digest"
            )
        edges, path_key = normalize_path(entry["path"])
        entry["path"] = edges
        recomputed = piece_digest(entry)
        if recomputed != entry["content_digest"]:
            raise ChengqiaoError(
                "invalid-submission",
                f"expected[{index}] content digest does not match its text",
                {"piece_id": piece_id},
            )
        if piece_id in piece_ids:
            raise ChengqiaoError(
                "invalid-submission", f"duplicate expected piece_id {piece_id}"
            )
        slot = (entry["orig_no"], path_key)
        if slot in slots:
            raise ChengqiaoError(
                "invalid-submission",
                f"duplicate expected path {path_key!r} for original {entry['orig_no']!r}",
            )
        piece_ids.add(piece_id)
        slots.add(slot)
        entry["path_key"] = path_key
        normalized.append(entry)

    computed_manifest = manifest_digest(receipt_id, batch, rule_id, normalized)
    if computed_manifest != given_digest:
        raise ChengqiaoError(
            "invalid-submission",
            "manifest digest does not match the submission receipt text",
            {"receipt_id": receipt_id},
        )

    return {
        "receipt_id": receipt_id,
        "submission_id": submission_id,
        "rule_id": rule_id,
        "batch": batch,
        "manifest_digest": given_digest,
        "expected": normalized,
        "manifest": build_manifest(receipt_id, batch, rule_id, normalized),
    }
