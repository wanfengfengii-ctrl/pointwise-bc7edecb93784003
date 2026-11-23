"""Frozen named-rule parsing and validation.

A rule is a finite directed acyclic graph of operations.  Each operation is
either a *terminal* node carrying a category, or a *branch* node carrying a
non-negative integer limit and exactly the two named edges ``left`` and
``right``.  Passing an amount through a branch is deterministic:

    the left edge carries  min(incoming_grams, limit)
    the right edge carries incoming_grams - left

Freezing rejects directed cycles (including self loops) and edges that point
at nodes outside the same rule.
"""

from __future__ import annotations

from .canon import digest, is_nonempty_str, is_nonneg_int
from .errors import ChengqiaoError

EDGES = ("L", "R")
EDGE_ORDER = {"L": 0, "R": 1}


def _fail(code: str, message: str, details=None):
    raise ChengqiaoError(code, message, details)


def parse_rule_spec(raw) -> dict:
    """Validate a rule document and return its normalized, digestable form."""
    if not isinstance(raw, dict):
        _fail("invalid-rule", "rule document must be a JSON object")
    unknown = set(raw) - {"rule_id", "nodes"}
    if unknown:
        _fail("invalid-rule", f"unknown rule fields: {sorted(unknown)}")

    rule_id = raw.get("rule_id")
    if not is_nonempty_str(rule_id):
        _fail("invalid-rule", "rule_id must be a non-empty string")

    nodes_raw = raw.get("nodes")
    if isinstance(nodes_raw, list):
        nodes: dict[str, dict] = {}
        for index, entry in enumerate(nodes_raw):
            if not isinstance(entry, dict) or not is_nonempty_str(entry.get("name")):
                _fail("invalid-rule", f"nodes[{index}] needs a non-empty name")
            name = entry["name"]
            if name in nodes:
                _fail("invalid-rule", f"duplicate node name {name!r}")
            nodes[name] = {key: value for key, value in entry.items() if key != "name"}
    elif isinstance(nodes_raw, dict):
        nodes = dict(nodes_raw)
    else:
        _fail("invalid-rule", "nodes must be an object or a list")

    if not nodes:
        _fail("invalid-rule", "rule needs at least one node")

    normalized: dict[str, dict] = {}
    for name, spec in nodes.items():
        if not is_nonempty_str(name):
            _fail("invalid-rule", "node names must be non-empty strings")
        if not isinstance(spec, dict):
            _fail("invalid-rule", f"node {name!r} must be an object")

        if "category" in spec:
            extra = set(spec) - {"category"}
            if extra:
                _fail(
                    "invalid-rule",
                    f"terminal node {name!r} cannot carry {sorted(extra)}",
                )
            if not is_nonempty_str(spec["category"]):
                _fail("invalid-rule", f"terminal node {name!r} needs a category")
            normalized[name] = {"category": spec["category"]}
            continue

        extra = set(spec) - {"limit", "left", "right"}
        if extra:
            _fail("invalid-rule", f"unknown fields on node {name!r}: {sorted(extra)}")
        for field in ("limit", "left", "right"):
            if field not in spec:
                _fail("invalid-rule", f"branch node {name!r} is missing {field}")
        if not is_nonneg_int(spec["limit"]):
            _fail(
                "invalid-rule",
                f"node {name!r} limit must be a non-negative integer",
            )
        for edge, target in (("left", spec["left"]), ("right", spec["right"])):
            if not is_nonempty_str(target):
                _fail("invalid-rule", f"node {name!r} {edge} edge must name a node")
        normalized[name] = {
            "limit": spec["limit"],
            "left": spec["left"],
            "right": spec["right"],
        }

    # Bad references: every named edge must point inside the same rule.
    missing = []
    for name, spec in normalized.items():
        if "category" in spec:
            continue
        for target in (spec["left"], spec["right"]):
            if target not in normalized:
                missing.append({"node": name, "target": target})
    if missing:
        _fail("bad-reference", "rule edges point at unknown nodes", missing)

    # Reject cycles (including self loops) using Kahn's algorithm.
    indegree = {name: 0 for name in normalized}
    for spec in normalized.values():
        if "category" in spec:
            continue
        for target in (spec["left"], spec["right"]):
            indegree[target] += 1
    queue = sorted(name for name, degree in indegree.items() if degree == 0)
    seen = 0
    while queue:
        name = queue.pop(0)
        seen += 1
        spec = normalized[name]
        if "category" in spec:
            continue
        for target in (spec["left"], spec["right"]):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if seen != len(normalized):
        _fail("cycle", "rule graph contains a directed cycle")

    return {"rule_id": rule_id, "nodes": normalized}


def rule_digest(rule: dict) -> str:
    return digest({"rule_id": rule["rule_id"], "nodes": rule["nodes"]})


def is_terminal(rule: dict, node: str) -> bool:
    return "category" in rule["nodes"][node]


def category_of(rule: dict, node: str) -> str:
    return rule["nodes"][node]["category"]


def path_sort_key(edges) -> tuple:
    """Left edge before right edge at every decision: DFS pre-order."""
    return tuple(EDGE_ORDER[edge] for edge in edges)
