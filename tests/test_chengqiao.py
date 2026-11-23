"""End-to-end tests for the two-store night-shift reconciliation."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from chengqiao.account_store import AccountStore
from chengqiao.errors import ChengqiaoError
from chengqiao.model import make_piece_payload
from chengqiao.process_store import ProcessStore

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

EXPECTED_PIECES = [
    ("LL", "Z", 0),
    ("LRL", "D", 2),
    ("LRR", "E", 3),
    ("RL", "Z", 0),
    ("RRL", "D", 2),
    ("RRR", "E", 5),
]


class StoreCase(unittest.TestCase):
    """Shared temp-directory plumbing; both stores always live apart."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.process_db = os.path.join(self.tmp.name, "process.db")
        self.account_db = os.path.join(self.tmp.name, "account.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def process(self) -> ProcessStore:
        return ProcessStore(self.process_db)

    def account(self) -> AccountStore:
        return AccountStore(self.account_db)

    def walk_all(self, store: ProcessStore, receipt_id: str) -> None:
        while True:
            ready = store.ready_items(receipt_id)["ready"]
            if not ready:
                return
            for item in ready:
                store.advance(receipt_id, item["orig_no"], item["path"])

    def submit_sample(self, grams: int = 12):
        process = self.process()
        process.freeze_rule(SAMPLE_RULE)
        accepted = process.accept_order(
            "B1", "R", [{"orig_no": "W1", "start": "R", "grams": grams}]
        )
        receipt_id = accepted["receipt_id"]
        self.walk_all(process, receipt_id)
        submission = process.submit(receipt_id)["submission"]
        return process, receipt_id, submission


class RuleTests(StoreCase):
    def test_freeze_is_idempotent_for_same_content(self):
        store = self.process()
        first = store.freeze_rule(SAMPLE_RULE)
        second = store.freeze_rule(SAMPLE_RULE)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["rule_digest"], second["rule_digest"])

    def test_freeze_rejects_same_id_with_new_content(self):
        store = self.process()
        store.freeze_rule(SAMPLE_RULE)
        changed = json.loads(json.dumps(SAMPLE_RULE))
        changed["nodes"]["M"]["limit"] = 3
        with self.assertRaises(ChengqiaoError) as caught:
            store.freeze_rule(changed)
        self.assertEqual(caught.exception.code, "rule-frozen")

    def test_freeze_rejects_cycles(self):
        cyclic = {
            "rule_id": "C",
            "nodes": {
                "X": {"limit": 1, "left": "Y", "right": "Z"},
                "Y": {"limit": 1, "left": "X", "right": "Z"},
                "Z": {"category": "z"},
            },
        }
        with self.assertRaises(ChengqiaoError) as caught:
            self.process().freeze_rule(cyclic)
        self.assertEqual(caught.exception.code, "cycle")

    def test_freeze_rejects_self_loop(self):
        loop = {
            "rule_id": "L",
            "nodes": {
                "X": {"limit": 1, "left": "X", "right": "Z"},
                "Z": {"category": "z"},
            },
        }
        with self.assertRaises(ChengqiaoError) as caught:
            self.process().freeze_rule(loop)
        self.assertEqual(caught.exception.code, "cycle")

    def test_freeze_rejects_bad_reference(self):
        broken = {
            "rule_id": "B",
            "nodes": {
                "X": {"limit": 1, "left": "Z", "right": "missing"},
                "Z": {"category": "z"},
            },
        }
        with self.assertRaises(ChengqiaoError) as caught:
            self.process().freeze_rule(broken)
        self.assertEqual(caught.exception.code, "bad-reference")

    def test_freeze_rejects_negative_limit(self):
        bad = {
            "rule_id": "N",
            "nodes": {
                "X": {"limit": -1, "left": "Z", "right": "Z"},
                "Z": {"category": "z"},
            },
        }
        with self.assertRaises(ChengqiaoError) as caught:
            self.process().freeze_rule(bad)
        self.assertEqual(caught.exception.code, "invalid-rule")

    def test_order_for_unknown_rule_is_refused(self):
        with self.assertRaises(ChengqiaoError) as caught:
            self.process().accept_order(
                "B1", "nope", [{"orig_no": "W1", "start": "R", "grams": 1}]
            )
        self.assertEqual(caught.exception.code, "unknown-rule")


class OrderTests(StoreCase):
    def test_duplicate_original_number_in_one_batch_is_refused(self):
        store = self.process()
        store.freeze_rule(SAMPLE_RULE)
        with self.assertRaises(ChengqiaoError) as caught:
            store.accept_order(
                "B1",
                "R",
                [
                    {"orig_no": "W1", "start": "R", "grams": 1},
                    {"orig_no": "W1", "start": "R", "grams": 2},
                ],
            )
        self.assertEqual(caught.exception.code, "duplicate-orig")

    def test_same_parameters_reissue_same_receipt(self):
        store = self.process()
        store.freeze_rule(SAMPLE_RULE)
        items = [{"orig_no": "W1", "start": "R", "grams": 12}]
        first = store.accept_order("B1", "R", items)
        second = store.accept_order("B1", "R", items)
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        self.assertFalse(first["reissued"])
        self.assertTrue(second["reissued"])

    def test_different_parameters_same_batch_is_refused(self):
        store = self.process()
        store.freeze_rule(SAMPLE_RULE)
        store.accept_order("B1", "R", [{"orig_no": "W1", "start": "R", "grams": 12}])
        with self.assertRaises(ChengqiaoError) as caught:
            store.accept_order(
                "B1", "R", [{"orig_no": "W1", "start": "R", "grams": 9}]
            )
        self.assertEqual(caught.exception.code, "param-conflict")

    def test_negative_grams_is_refused(self):
        store = self.process()
        store.freeze_rule(SAMPLE_RULE)
        with self.assertRaises(ChengqiaoError) as caught:
            store.accept_order(
                "B1", "R", [{"orig_no": "W1", "start": "R", "grams": -1}]
            )
        self.assertEqual(caught.exception.code, "invalid-order")

    def test_hundreds_of_grams_stay_integers(self):
        store = self.process()
        store.freeze_rule(SAMPLE_RULE)
        accepted = store.accept_order(
            "B1", "R", [{"orig_no": "W1", "start": "R", "grams": 12}]
        )
        self.assertIsInstance(accepted["order"]["weighings"][0]["grams"], int)


class TraversalTests(StoreCase):
    def test_sample_produces_expected_endpoints(self):
        process, receipt_id, _ = self.submit_sample()
        pieces = process.pieces(receipt_id)["pieces"]
        got = [("".join(p["path"]), p["category"], p["grams"]) for p in pieces]
        self.assertEqual(got, EXPECTED_PIECES)

    def test_terminal_entries_carry_full_receipt_fields(self):
        process, receipt_id, _ = self.submit_sample()
        piece = process.pieces(receipt_id)["pieces"][0]
        for field in ("receipt_id", "rule_id", "orig_no", "path", "category", "grams"):
            self.assertIn(field, piece)
        self.assertEqual(piece["receipt_id"], receipt_id)
        self.assertEqual(piece["rule_id"], "R")
        self.assertEqual(piece["orig_no"], "W1")

    def test_merge_keeps_two_separate_deltas(self):
        process, receipt_id, _ = self.submit_sample()
        deltas = [
            p for p in process.pieces(receipt_id)["pieces"] if p["category"] == "D"
        ]
        self.assertEqual(len(deltas), 2)
        self.assertEqual(sorted("".join(p["path"]) for p in deltas), ["LRL", "RRL"])

    def test_zero_grams_are_walked_to_terminals(self):
        process = self.process()
        process.freeze_rule(SAMPLE_RULE)
        accepted = process.accept_order(
            "B1", "R", [{"orig_no": "W0", "start": "R", "grams": 0}]
        )
        self.walk_all(process, accepted["receipt_id"])
        pieces = process.pieces(accepted["receipt_id"])["pieces"]
        self.assertEqual(len(pieces), 6)
        self.assertTrue(all(p["grams"] == 0 for p in pieces))

    def test_weighing_starting_on_terminal_is_recorded_directly(self):
        process = self.process()
        process.freeze_rule(SAMPLE_RULE)
        accepted = process.accept_order(
            "B1", "R", [{"orig_no": "W9", "start": "D", "grams": 7}]
        )
        pieces = process.pieces(accepted["receipt_id"])["pieces"]
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0]["path"], [])
        self.assertEqual(pieces[0]["category"], "D")
        self.assertEqual(pieces[0]["grams"], 7)
        self.assertTrue(process.ready_items(accepted["receipt_id"])["status"]["finished"])

    def test_retry_of_committed_advance_returns_original(self):
        process = self.process()
        process.freeze_rule(SAMPLE_RULE)
        receipt_id = process.accept_order(
            "B1", "R", [{"orig_no": "W1", "start": "R", "grams": 12}]
        )["receipt_id"]
        first = process.advance(receipt_id, "W1", "")
        second = process.advance(receipt_id, "W1", "")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["result"], second["result"])
        # Only one copy of each child exists.
        self.assertEqual(process.ready_items(receipt_id)["status"]["pending_ready"], 2)

    def test_advance_of_unknown_path_is_refused(self):
        process, receipt_id, _ = self.submit_sample()
        with self.assertRaises(ChengqiaoError) as caught:
            process.advance(receipt_id, "W1", "RLR")
        self.assertEqual(caught.exception.code, "not-ready")

    def test_incomplete_order_cannot_be_submitted(self):
        process = self.process()
        process.freeze_rule(SAMPLE_RULE)
        receipt_id = process.accept_order(
            "B1", "R", [{"orig_no": "W1", "start": "R", "grams": 12}]
        )["receipt_id"]
        with self.assertRaises(ChengqiaoError) as caught:
            process.submit(receipt_id)
        self.assertEqual(caught.exception.code, "not-complete")


class SubmissionTests(StoreCase):
    def test_submission_is_original_and_replayable(self):
        process, receipt_id, submission = self.submit_sample()
        again = process.submit(receipt_id)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["submission"], submission)
        self.assertEqual(len(submission["expected"]), 6)
        self.assertTrue(submission["manifest_digest"])

    def test_tampered_manifest_is_refused(self):
        _, _, submission = self.submit_sample()
        tampered = json.loads(json.dumps(submission))
        tampered["expected"][0]["grams"] = 99
        with self.assertRaises(ChengqiaoError) as caught:
            self.account().declare(tampered)
        self.assertEqual(caught.exception.code, "invalid-submission")

    def test_receipt_for_other_rule_is_refused(self):
        _, _, submission = self.submit_sample()
        tampered = json.loads(json.dumps(submission))
        tampered["expected"][0]["rule_id"] = "other"
        with self.assertRaises(ChengqiaoError) as caught:
            self.account().declare(tampered)
        self.assertEqual(caught.exception.code, "invalid-submission")


class AccountTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.process_store, self.receipt_id, self.submission = self.submit_sample()
        self.account_store = self.account()
        self.account_store.declare(self.submission)

    def payload(self, index: int) -> dict:
        return make_piece_payload(
            self.submission["expected"][index], self.submission["submission_id"]
        )

    def post_all(self):
        for index in (3, 0, 5, 1, 4, 2):
            self.account_store.post_piece(self.payload(index))

    def test_piece_before_declaration_is_refused(self):
        fresh = AccountStore(os.path.join(self.tmp.name, "fresh.db"))
        with self.assertRaises(ChengqiaoError) as caught:
            fresh.post_piece(self.payload(0))
        self.assertEqual(caught.exception.code, "not-declared")

    def test_out_of_order_posting_books_each_once(self):
        self.post_all()
        status = self.account_store.lookup(self.receipt_id)["status"]
        self.assertEqual(status["received_count"], 6)
        self.assertEqual(status["missing_count"], 0)

    def test_lost_reply_repost_is_idempotent(self):
        first = self.account_store.post_piece(self.payload(0))
        second = self.account_store.post_piece(self.payload(0))
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        status = self.account_store.lookup(self.receipt_id)["status"]
        self.assertEqual(status["received_count"], 1)

    def test_misbound_piece_is_refused(self):
        payload = self.payload(0)
        payload["submission_id"] = "SUB-OTHER"
        with self.assertRaises(ChengqiaoError) as caught:
            self.account_store.post_piece(payload)
        self.assertEqual(caught.exception.code, "misbound")

    def test_piece_with_different_text_is_refused(self):
        payload = self.payload(0)
        payload["grams"] = payload["grams"] + 1
        with self.assertRaises(ChengqiaoError) as caught:
            self.account_store.post_piece(payload)
        self.assertEqual(caught.exception.code, "invalid-piece")

    def test_well_formed_but_changed_piece_text_is_refused(self):
        # Re-sign the altered piece so only the expectation check can catch it.
        from chengqiao.model import PIECE_FIELDS, piece_digest

        payload = self.payload(0)
        payload["grams"] = 1
        entry = {field: payload[field] for field in PIECE_FIELDS}
        payload["content_digest"] = piece_digest(entry)
        with self.assertRaises(ChengqiaoError) as caught:
            self.account_store.post_piece(payload)
        self.assertEqual(caught.exception.code, "text-conflict")

    def test_piece_for_undeclared_path_is_refused(self):
        payload = self.payload(0)
        payload["path"] = list("RRRR")
        from chengqiao.model import PIECE_FIELDS, piece_digest

        entry = {field: payload[field] for field in PIECE_FIELDS}
        payload["content_digest"] = piece_digest(entry)
        with self.assertRaises(ChengqiaoError) as caught:
            self.account_store.post_piece(payload)
        self.assertEqual(caught.exception.code, "text-conflict")

    def test_two_conflicting_texts_for_one_occurrence_is_refused(self):
        self.account_store.post_piece(self.payload(0))
        altered = self.payload(0)
        altered["submission_id"] = self.submission["submission_id"]
        # Same occurrence, different submission id text: conflict before binding.
        altered["submission_id"] = "SUB-OTHER"
        with self.assertRaises(ChengqiaoError) as caught:
            self.account_store.post_piece(altered)
        self.assertIn(caught.exception.code, {"misbound", "text-conflict"})

    def test_seal_with_missing_deltas_stays_pending(self):
        for index in (1, 2):
            self.account_store.post_piece(self.payload(index))
        with self.assertRaises(ChengqiaoError) as caught:
            self.account_store.seal(self.receipt_id)
        self.assertEqual(caught.exception.code, "pending")
        missing = caught.exception.details["missing"]
        self.assertEqual(
            sorted("".join(m["path"]) for m in missing), ["LL", "RL", "RRL", "RRR"]
        )
        found = self.account_store.lookup(self.receipt_id)
        self.assertEqual(found["state"], "pending")
        self.assertIsNone(found["status"]["result_id"])

    def test_failed_seal_leaves_no_result_behind(self):
        with self.assertRaises(ChengqiaoError):
            self.account_store.seal(self.receipt_id)
        self.assertEqual(self.account_store.lookup(self.receipt_id)["state"], "pending")
        self.assertEqual(
            self.account_store.lookup(f"SEAL-{self.receipt_id}")["state"], "pending"
        )

    def test_result_after_all_paths_arrive(self):
        self.post_all()
        sealed = self.account_store.seal(self.receipt_id)
        self.assertFalse(sealed["replayed"])
        result = sealed["result"]
        self.assertEqual(result["result_id"], f"SEAL-{self.receipt_id}")
        self.assertEqual(result["rule_id"], "R")
        self.assertEqual(result["receipt"]["manifest_digest"],
                         self.submission["manifest_digest"])
        detail = [("".join(p["path"]), p["category"], p["grams"])
                  for p in result["detail"]]
        self.assertEqual(detail, EXPECTED_PIECES)
        self.assertEqual(
            result["totals"],
            [{"category": "D", "grams": 4},
             {"category": "E", "grams": 8},
             {"category": "Z", "grams": 0}],
        )

    def test_detail_is_ordered_by_original_then_edge_order(self):
        process = ProcessStore(self.process_db)
        process.freeze_rule(SAMPLE_RULE)
        accepted = process.accept_order(
            "B2",
            "R",
            [
                {"orig_no": "W2", "start": "R", "grams": 12},
                {"orig_no": "W1", "start": "R", "grams": 12},
            ],
        )
        receipt_id = accepted["receipt_id"]
        self.walk_all(process, receipt_id)
        submission = process.submit(receipt_id)["submission"]
        account = AccountStore(os.path.join(self.tmp.name, "multi.db"))
        account.declare(submission)
        for entry in reversed(submission["expected"]):
            account.post_piece(make_piece_payload(entry, submission["submission_id"]))
        result = account.seal(receipt_id)["result"]
        keys = [(p["orig_no"], "".join(p["path"])) for p in result["detail"]]
        self.assertEqual(
            keys,
            [("W2", "LL"), ("W2", "LRL"), ("W2", "LRR"), ("W2", "RL"),
             ("W2", "RRL"), ("W2", "RRR"), ("W1", "LL"), ("W1", "LRL"),
             ("W1", "LRR"), ("W1", "RL"), ("W1", "RRL"), ("W1", "RRR")],
        )

    def test_seal_retry_signs_only_once(self):
        self.post_all()
        first = self.account_store.seal(self.receipt_id)
        second = self.account_store.seal(self.receipt_id)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["result"], second["result"])
        self.assertEqual(second["result"]["result_id"], first["result"]["result_id"])

    def test_declare_is_idempotent_and_conflicts_on_new_text(self):
        again = self.account_store.declare(self.submission)
        self.assertTrue(again["replayed"])
        altered = json.loads(json.dumps(self.submission))
        altered["batch"] = "other"
        with self.assertRaises(ChengqiaoError) as caught:
            self.account_store.declare(altered)
        self.assertEqual(caught.exception.code, "invalid-submission")


class ReopenTests(StoreCase):
    def test_account_survives_process_store_closure(self):
        process, receipt_id, submission = self.submit_sample()
        account = self.account()
        account.declare(submission)
        for entry in submission["expected"]:
            account.post_piece(make_piece_payload(entry, submission["submission_id"]))
        result = account.seal(receipt_id)["result"]

        process.close()
        del process

        reopened = AccountStore(self.account_db)
        by_receipt = reopened.lookup(receipt_id)
        by_result = reopened.lookup(result["result_id"])
        self.assertEqual(by_receipt["state"], "sealed")
        self.assertEqual(by_result["state"], "sealed")
        self.assertEqual(by_receipt["result"], result)

        self.assertEqual(reopened.lookup("NO-SUCH")["state"], "unknown")

    def test_pending_and_unknown_are_distinguished(self):
        process, receipt_id, submission = self.submit_sample()
        account = self.account()
        account.declare(submission)
        account.post_piece(
            make_piece_payload(submission["expected"][0], submission["submission_id"])
        )
        process.close()
        del process

        reopened = AccountStore(self.account_db)
        pending = reopened.lookup(receipt_id)
        self.assertEqual(pending["state"], "pending")
        self.assertEqual(len(pending["missing"]), 5)
        self.assertEqual(reopened.lookup("B1-0404")["state"], "unknown")

    def test_processing_state_survives_reopen(self):
        process, receipt_id, submission = self.submit_sample()
        process.close()
        del process
        reopened = ProcessStore(self.process_db)
        again = reopened.submit(receipt_id)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["submission"], submission)


class IsolatedStoreTests(StoreCase):
    def test_account_end_reads_no_processing_tables(self):
        _, _, submission = self.submit_sample()
        account = self.account()
        account.declare(submission)
        # Account DB is self-contained: it never needs the process file.
        os.rename(self.process_db, self.process_db + ".moved")
        for entry in submission["expected"]:
            account.post_piece(make_piece_payload(entry, submission["submission_id"]))
        sealed = account.seal(submission["receipt_id"])
        self.assertEqual(sealed["result"]["total_grams"], 12)


if __name__ == "__main__":
    unittest.main()
