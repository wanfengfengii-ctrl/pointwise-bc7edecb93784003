"""Command-line surface for the night-shift reconciliation.

Two stores are named on every call so the CLI never assumes they live
together: ``--process DB`` and ``--account DB``.  Each subcommand performs one
business action and prints one JSON object on stdout.  A rejected action
prints ``{"error": ..., "message": ...}`` on stderr and exits 2.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional

from .account_store import AccountStore
from .errors import ChengqiaoError
from .process_store import ProcessStore


def _emit(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")


def _read_json(path: Optional[str]) -> Any:
    if path is None or path == "-":
        text = sys.stdin.read()
    else:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ChengqiaoError("invalid-json", f"input is not valid JSON: {exc}") from None


def _process(args) -> ProcessStore:
    return ProcessStore(args.process)


def _account(args) -> AccountStore:
    return AccountStore(args.account)


# --------------------------------------------------------------------- actions
def cmd_freeze_rule(args) -> dict:
    return _process(args).freeze_rule(_read_json(args.file))


def cmd_accept_order(args) -> dict:
    order = _read_json(args.file)
    if not isinstance(order, dict):
        raise ChengqiaoError("invalid-order", "order document must be a JSON object")
    return _process(args).accept_order(
        order.get("batch"), order.get("rule_id"), order.get("weighings")
    )


def cmd_ready(args) -> dict:
    return _process(args).ready_items(args.receipt_id)


def cmd_advance(args) -> dict:
    path = args.path
    return _process(args).advance(args.receipt_id, args.orig_no, path)


def cmd_pieces(args) -> dict:
    return _process(args).pieces(args.receipt_id)


def cmd_submit(args) -> dict:
    found = _process(args).submit(args.receipt_id)
    found["receipt"] = found.pop("submission")
    return found


def cmd_declare(args) -> dict:
    return _account(args).declare(_read_json(args.file))


def cmd_post_piece(args) -> dict:
    return _account(args).post_piece(_read_json(args.file))


def cmd_seal(args) -> dict:
    return _account(args).seal(args.receipt_id)


def cmd_lookup(args) -> dict:
    return _account(args).lookup(args.ref)


def cmd_result(args) -> dict:
    return _account(args).result(args.result_id)


# ---------------------------------------------------------------------- wiring
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chengqiao",
        description="Night-shift reconciliation over two independent SQLite stores.",
    )
    stores = parser.add_argument_group("store paths")
    stores.add_argument("--process", metavar="DB", help="processing SQLite file")
    stores.add_argument("--account", metavar="DB", help="account SQLite file")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("freeze-rule", help="freeze one named rule (rejects cycles/bad refs)")
    p.add_argument("file", nargs="?", default="-", help="rule JSON file (default stdin)")
    p.set_defaults(func=cmd_freeze_rule)

    p = sub.add_parser("accept-order", help="accept a batch of ordered weighings")
    p.add_argument("file", nargs="?", default="-", help="order JSON file (default stdin)")
    p.set_defaults(func=cmd_accept_order)

    p = sub.add_parser("ready", help="list ready processing items")
    p.add_argument("receipt_id")
    p.set_defaults(func=cmd_ready)

    p = sub.add_parser("advance", help="advance one ready item along its full path")
    p.add_argument("receipt_id")
    p.add_argument("orig_no")
    p.add_argument("path", help="full path of named edges so far, e.g. '' or 'LR'")
    p.set_defaults(func=cmd_advance)

    p = sub.add_parser("pieces", help="list endpoint pieces produced so far")
    p.add_argument("receipt_id")
    p.set_defaults(func=cmd_pieces)

    p = sub.add_parser("submit", help="build the processing-end submission receipt")
    p.add_argument("receipt_id")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("declare", help="accept a submission receipt at the account end")
    p.add_argument("file", nargs="?", default="-")
    p.set_defaults(func=cmd_declare)

    p = sub.add_parser("post-piece", help="book one endpoint original")
    p.add_argument("file", nargs="?", default="-")
    p.set_defaults(func=cmd_post_piece)

    p = sub.add_parser("seal", help="seal the account result for one receipt")
    p.add_argument("receipt_id")
    p.set_defaults(func=cmd_seal)

    p = sub.add_parser("lookup", help="look up by receipt id or result id")
    p.add_argument("ref")
    p.set_defaults(func=cmd_lookup)

    p = sub.add_parser("result", help="print one sealed result")
    p.add_argument("result_id")
    p.set_defaults(func=cmd_result)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    needs_process = {
        "freeze-rule", "accept-order", "ready", "advance", "pieces", "submit",
    }
    needs_account = {
        "declare", "post-piece", "seal", "lookup", "result",
    }
    if args.command in needs_process and not args.process:
        parser.error(f"{args.command} needs --process DB")
    if args.command in needs_account and not args.account:
        parser.error(f"{args.command} needs --account DB")

    try:
        _emit(args.func(args))
    except ChengqiaoError as exc:
        sys.stderr.write(json.dumps(exc.as_dict(), ensure_ascii=False, sort_keys=True) + "\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
