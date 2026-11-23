"""End-to-end demonstration of the night-shift reconciliation.

Runs the sample rule from the task statement, walks every ready item, hands
the processing-end receipt and each endpoint original to the account end in a
deliberately shuffled order (with a lost-reply retry), seals the result, and
then closes the processing store and re-opens only the account store to show
both lookup routes and the unknown/pending distinction.

Everything is printed as readable steps so ``verify`` can assert on it.
"""

from __future__ import annotations

import json
import os
import tempfile

from .account_store import AccountStore
from .errors import ChengqiaoError
from .model import make_piece_payload
from .process_store import ProcessStore

SAMPLE_RULE = {
    "rule_id": "R",
    "nodes": {
        "R": {"limit": 5, "left": "A", "right": "B"},
        "A": {"limit": 0, "left": "Z", "right": "M"},
        "B": {"limit": 0, "left": "Z", "right": "M"},
        "M": {"limit": 2, "left": "D", "right": "E"},
        "Z": {"category": "Z"},
        "D": {"category": "D"},
        "E": {"category": "E"},
    },
}


def _show(label: str, obj) -> None:
    print(f"\n[{label}]")
    print(json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2))


def walk_all(store: ProcessStore, receipt_id: str) -> list[dict]:
    """Advance every ready item until nothing is ready; return the pieces."""
    while True:
        view = store.ready_items(receipt_id)
        if not view["ready"]:
            break
        for item in view["ready"]:
            store.advance(receipt_id, item["orig_no"], item["path"])
    return store.pieces(receipt_id)["pieces"]


def run(workdir: str | None = None) -> dict:
    workdir = workdir or tempfile.mkdtemp(prefix="chengqiao-demo-")
    process_db = os.path.join(workdir, "process.db")
    account_db = os.path.join(workdir, "account.db")

    process = ProcessStore(process_db)
    account = AccountStore(account_db)

    _show("freeze rule", process.freeze_rule(SAMPLE_RULE))

    order = {
        "batch": "B1",
        "rule_id": "R",
        "weighings": [{"orig_no": "W1", "start": "R", "grams": 12}],
    }
    accepted = process.accept_order(order["batch"], order["rule_id"], order["weighings"])
    _show("accept order", accepted)
    receipt_id = accepted["receipt_id"]

    again = process.accept_order(order["batch"], order["rule_id"], order["weighings"])
    _show("re-accept with same parameters", {"receipt_id": again["receipt_id"],
                                             "reissued": again["reissued"]})

    print("\n[advance every ready item]")
    pieces = walk_all(process, receipt_id)
    for piece in pieces:
        print(f"  path={''.join(piece['path']):4s} category={piece['category']:2s}"
              f" grams={piece['grams']}")

    submission = process.submit(receipt_id)["submission"]
    _show("processing-end submission (manifest)",
          {"receipt_id": submission["receipt_id"],
           "submission_id": submission["submission_id"],
           "piece_count": len(submission["expected"])})

    # Retrying the submission after a lost reply yields the same original.
    replay = process.submit(receipt_id)
    _show("retry submission", {"replayed": replay["replayed"],
                               "same_payload": replay["submission"] == submission})

    account.declare(submission)

    # Post originals out of order; post one twice (lost-reply retry).
    post_order = [3, 0, 5, 1, 4, 2]
    print("\n[post endpoint originals out of order]")
    for index in post_order:
        payload = make_piece_payload(submission["expected"][index],
                                     submission["submission_id"])
        reply = account.post_piece(payload)
        print(f"  post path={''.join(payload['path']):4s} replayed={reply['replayed']}")
    first = make_piece_payload(submission["expected"][3], submission["submission_id"])
    retry = account.post_piece(first)
    print(f"  retry path={''.join(first['path']):4s} replayed={retry['replayed']}")

    # Sealing before completion is refused and leaves nothing behind.
    partial_account = AccountStore(os.path.join(workdir, "account-partial.db"))
    partial_account.declare(submission)
    for index in (0, 1):
        partial_account.post_piece(
            make_piece_payload(submission["expected"][index],
                               submission["submission_id"])
        )
    try:
        partial_account.seal(receipt_id)
    except ChengqiaoError as exc:
        _show("seal before all paths arrive (refused)",
              {"error": exc.code, "missing": len(exc.details["missing"])})
    sealed = account.seal(receipt_id)
    result = sealed["result"]
    _show("sealed result",
          {"result_id": result["result_id"],
           "rule_id": result["rule_id"],
           "detail": [[p["orig_no"], "".join(p["path"]), p["category"], p["grams"]]
                      for p in result["detail"]],
           "totals": result["totals"]})

    today = AccountStore(os.path.join(workdir, "account-today.db"))
    today.declare(submission)
    for index in (0, 1, 2):
        today.post_piece(
            make_piece_payload(submission["expected"][index],
                               submission["submission_id"])
        )

    resealed = account.seal(receipt_id)
    _show("retry seal", {"replayed": resealed["replayed"],
                         "same_result": resealed["result"] == result})

    # Close the processing store; only the account store is re-opened.
    process.close()
    del process

    reopened = AccountStore(account_db)
    _show("reopen account, lookup by receipt id",
          _brief(reopened.lookup(receipt_id)))
    _show("reopen account, lookup by result id",
          _brief(reopened.lookup(result["result_id"])))
    _show("reopen account, unknown id", _brief(reopened.lookup("B1-9999")))

    pending = AccountStore(os.path.join(workdir, "account-today.db"))
    _show("reopen a half-booked account, lookup is pending",
          _brief(pending.lookup(receipt_id)))

    return {
        "receipt_id": receipt_id,
        "result_id": result["result_id"],
        "pieces": [[p["orig_no"], "".join(p["path"]), p["category"], p["grams"]]
                   for p in pieces],
        "totals": result["totals"],
        "process_db": process_db,
        "account_db": account_db,
    }


def _brief(found: dict) -> dict:
    if found["state"] == "sealed":
        return {"state": "sealed", "ref": found["ref"],
                "result_id": found["result"]["result_id"]}
    if found["state"] == "pending":
        return {"state": "pending", "ref": found["ref"],
                "missing": ["".join(m["path"]) for m in found["missing"]]}
    return {"state": "unknown", "ref": found["ref"]}


def main() -> int:
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
