"""Single verification entry point (``python3 -B -m chengqiao.verify``).

Runs, in order, and exits non-zero on the first failure:

1. the full unit/integration suite from ``tests/``;
2. a CLI-level scenario that exercises lost-reply retries ("失答补齐"):
   a repeated advance, a repeated submission, a repeated piece post and a
   repeated seal, each returning the stored original;
3. failure paths that must exit non-zero: a rejected freeze (cycle), an
   early seal with missing endpoint paths, an unknown lookup reference, and a
   misbound piece.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


class VerificationFailure(Exception):
    pass


def _cli(process_db: str, account_db: str, *argv: str, ok: bool = True,
         stdin: dict | None = None) -> dict:
    command = [sys.executable, "-B", "-m", "chengqiao"]
    if process_db:
        command += ["--process", process_db]
    if account_db:
        command += ["--account", account_db]
    command += list(argv)
    result = subprocess.run(
        command,
        cwd=ROOT,
        input=json.dumps(stdin) if stdin is not None else None,
        text=True,
        capture_output=True,
    )
    if ok and result.returncode != 0:
        raise VerificationFailure(
            f"command {argv} exited {result.returncode}: {result.stderr.strip()}"
        )
    if not ok and result.returncode == 0:
        raise VerificationFailure(f"command {argv} should have failed but exited 0")
    try:
        body = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError as exc:
        raise VerificationFailure(f"command {argv} printed non-JSON: {exc}") from None
    if not ok:
        try:
            body = json.loads(result.stderr) if result.stderr.strip() else body
        except json.JSONDecodeError:
            pass
    return body


def run_unit_tests() -> None:
    suite = unittest.defaultTestLoader.discover(
        start_dir=os.path.join(ROOT, "tests"), top_level_dir=ROOT
    )
    runner = unittest.TextTestRunner(verbosity=1, stream=sys.stderr)
    result = runner.run(suite)
    if not result.wasSuccessful():
        raise VerificationFailure("unit/integration suite failed")


def run_cli_scenario() -> None:
    with tempfile.TemporaryDirectory(prefix="chengqiao-verify-") as tmp:
        process_db = os.path.join(tmp, "process.db")
        account_db = os.path.join(tmp, "account.db")

        _cli(process_db, "", "freeze-rule", stdin=SAMPLE_RULE)
        order = {
            "batch": "B1",
            "rule_id": "R",
            "weighings": [{"orig_no": "W1", "start": "R", "grams": 12}],
        }
        accepted = _cli(process_db, "", "accept-order", stdin=order)
        receipt_id = accepted["receipt_id"]

        # Sanity: the sample must expand to the documented endpoints.
        state = _cli(process_db, "", "ready", receipt_id)
        while state["ready"]:
            for item in state["ready"]:
                _cli(process_db, "", "advance", receipt_id, item["orig_no"],
                     "".join(item["path"]))
            state = _cli(process_db, "", "ready", receipt_id)
        produced = _cli(process_db, "", "pieces", receipt_id)["pieces"]
        got = [("".join(p["path"]), p["category"], p["grams"]) for p in produced]
        expected = [("LL", "Z", 0), ("LRL", "D", 2), ("LRR", "E", 3),
                    ("RL", "Z", 0), ("RRL", "D", 2), ("RRR", "E", 5)]
        if got != expected:
            raise VerificationFailure(f"sample expansion mismatch: {got}")

        # Lost-reply retry on advance: same stored outcome, still six pieces.
        replay = _cli(process_db, "", "advance", receipt_id, "W1", "")
        if not replay["replayed"]:
            raise VerificationFailure("advance retry was not replayed")
        if len(_cli(process_db, "", "pieces", receipt_id)["pieces"]) != 6:
            raise VerificationFailure("advance retry duplicated endpoint pieces")

        submission = _cli(process_db, "", "submit", receipt_id)["receipt"]
        retry_sub = _cli(process_db, "", "submit", receipt_id)
        if not retry_sub["replayed"] or retry_sub["receipt"] != submission:
            raise VerificationFailure("submission retry changed the original")

        _cli("", account_db, "declare", stdin=submission)

        # Failure path: sealing before completion exits non-zero and is pending.
        early = _cli("", account_db, "seal", receipt_id, ok=False)
        if early.get("error") != "pending":
            raise VerificationFailure(f"early seal should be pending: {early}")
        if _cli("", account_db, "lookup", receipt_id)["state"] != "pending":
            raise VerificationFailure("early seal changed the account state")

        # Out-of-order posting with a lost-reply retry of one occurrence.
        from chengqiao.model import make_piece_payload

        for index in (3, 0, 5, 1, 4, 2):
            payload = make_piece_payload(
                submission["expected"][index], submission["submission_id"]
            )
            _cli("", account_db, "post-piece", stdin=payload)
        repeat = _cli(
            "", account_db, "post-piece", stdin=make_piece_payload(
                submission["expected"][0], submission["submission_id"])
        )
        if not repeat["replayed"]:
            raise VerificationFailure("piece retry was not replayed")
        if repeat["status"]["received_count"] != 6:
            raise VerificationFailure("piece retry double-booked an occurrence")

        # Failure path: misbound piece is refused.
        misbound = make_piece_payload(
            submission["expected"][0], submission["submission_id"]
        )
        misbound["submission_id"] = "SUB-OTHER"
        _cli("", account_db, "post-piece", stdin=misbound, ok=False)

        sealed = _cli("", account_db, "seal", receipt_id)
        if sealed["result"]["total_grams"] != 12:
            raise VerificationFailure("sealed total grams mismatch")
        resealed = _cli("", account_db, "seal", receipt_id)
        if not resealed["replayed"] or resealed["result"] != sealed["result"]:
            raise VerificationFailure("seal retry produced a new result")

        # Reopen the account store alone; the process store is gone.
        os.remove(process_db)
        by_receipt = _cli("", account_db, "lookup", receipt_id)
        by_result = _cli("", account_db, "lookup", sealed["result"]["result_id"])
        unknown = _cli("", account_db, "lookup", "B1-9999")
        if by_receipt["state"] != "sealed" or by_result["state"] != "sealed":
            raise VerificationFailure("reopened lookup lost the sealed result")
        if unknown["state"] != "unknown":
            raise VerificationFailure("unknown reference was not reported unknown")

        # Failure path: a cyclic rule is refused.
        cyclic = {
            "rule_id": "CYC",
            "nodes": {
                "X": {"limit": 1, "left": "Y", "right": "Z"},
                "Y": {"limit": 1, "left": "X", "right": "Z"},
                "Z": {"category": "z"},
            },
        }
        rejected = _cli(process_db, "", "freeze-rule", stdin=cyclic, ok=False)
        if rejected.get("error") != "cycle":
            raise VerificationFailure(f"cycle was not refused: {rejected}")


def main() -> int:
    try:
        run_unit_tests()
        run_cli_scenario()
    except VerificationFailure as exc:
        sys.stderr.write(f"VERIFY FAILED: {exc}\n")
        return 1
    except Exception as exc:  # any unexpected crash is a verification failure
        sys.stderr.write(f"VERIFY ERROR: {type(exc).__name__}: {exc}\n")
        return 1
    sys.stderr.write("VERIFY OK\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
