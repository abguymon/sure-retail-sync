"""Unit tests for sure-retail-sync core logic and HTTP API."""

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import requests

from server.amazon_parser import (
    allocate_splits,
    format_notes,
    items_for_charge,
    match_charges_to_sure,
)
from server.app import handle_amazon_plan, handle_amazon_sync, make_handler
from server.categorizer import NullCategorizer


class FakeSure:
    def __init__(self, txns=None, cats=None):
        self.txns = txns or []
        self.cats = cats or {"Groceries": "cat-groc", "Shopping": "cat-shop", "Kids": "cat-kids"}
        self.dry_run = False
        self.updated = []
        self.splits = []

    def spending_categories(self, exclude_names=None, include_parents=None):
        excluded = set(exclude_names or ())
        return {k: v for k, v in self.cats.items() if k not in excluded}

    def amazon_transactions(self, start, end):
        return self.txns

    def update_transaction(self, txn_id, **fields):
        if not self.dry_run:
            self.updated.append((txn_id, fields))

    def split_transaction(self, txn_id, splits):
        if not self.dry_run:
            self.splits.append((txn_id, splits))


class FakeCategorizer:
    name = "fake"

    def __init__(self, mapping):
        self.mapping = mapping

    def categorize(self, store, items, categories):
        return [self.mapping[it["title"]] for it in items]


class CoreTests(unittest.TestCase):
    def test_single_shipment_gift_card_discount_keeps_all_items(self):
        order = {
            "order_number": "111-1111111-1111111",
            "grand_total": 100.00,
            "items": [
                {"title": "Coffee Beans", "price": 40.0, "qty": 1, "shipment": 0},
                {"title": "USB Cable", "price": 60.0, "qty": 1, "shipment": 0},
            ],
        }
        # Only $50 hit the card because a $50 gift card covered half
        items = items_for_charge(order, 50.00)
        self.assertEqual(len(items), 2)

    def test_multi_shipment_picks_matching_shipment(self):
        order = {
            "order_number": "111-2222222-2222222",
            "grand_total": 106.00,  # 6% tax
            "items": [
                {"title": "Dog Food", "price": 50.0, "qty": 1, "shipment": 0},
                {"title": "Lego Set", "price": 50.0, "qty": 1, "shipment": 1},
            ],
        }
        items = items_for_charge(order, 53.00)
        self.assertEqual(len(items), 1)

    def test_allocate_splits_folds_tiny_share_and_preserves_cents(self):
        items = [
            {"title": "Main", "price": 95.0, "qty": 1},
            {"title": "Tiny", "price": 2.0, "qty": 1},
        ]
        shares = allocate_splits(9700, items, ["Shopping", "Groceries"], min_split_share=0.10)
        self.assertEqual(shares, {"Shopping": 9700})

        items2 = [
            {"title": "A", "price": 10.0, "qty": 1},
            {"title": "B", "price": 20.0, "qty": 1},
        ]
        shares2 = allocate_splits(3001, items2, ["Groceries", "Kids"], min_split_share=0.10)
        self.assertEqual(sum(shares2.values()), 3001)
        self.assertEqual(set(shares2.keys()), {"Groceries", "Kids"})

    def test_match_charges_respects_last4_and_date_window(self):
        charges = [
            {
                "completed_date": "2026-11-10",
                "grand_total": -45.67,
                "cents": -4567,
                "is_refund": False,
                "order_number": "111-0000000-0000001",
                "payment_method_last_4": "9345",
            }
        ]
        txns = [
            {
                "id": "wrong-card",
                "date": "2026-11-10",
                "signed_amount_cents": -4567,
                "account": {"name": "Venture (2510)"},
            },
            {
                "id": "right-card",
                "date": "2026-11-15",  # 5 days later (within 7d window)
                "signed_amount_cents": -4567,
                "account": {"name": "Chase Sapphire Preferred (9345)"},
            },
        ]
        matched = match_charges_to_sure(charges, txns, date_window_days=7)
        self.assertEqual(matched[0][1]["id"], "right-card")

    def test_plan_and_sync_split_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            sure = FakeSure(
                txns=[
                    {
                        "id": "txn-1",
                        "date": "2026-11-11",
                        "signed_amount_cents": -10000,
                        "account": {"name": "Chase (9345)"},
                        "notes": "",
                        "user_modified": False,
                        "split_parent": False,
                    }
                ]
            )
            charges = [
                {
                    "completed_date": "2026-11-10",
                    "grand_total": -100.0,
                    "cents": -10000,
                    "is_refund": False,
                    "order_number": "111-3333333-3333333",
                    "payment_method_last_4": "9345",
                }
            ]
            plan = handle_amazon_plan({"charges": charges, "days": 365}, sure, data_dir)
            self.assertEqual(plan["matched"], 1)
            self.assertEqual(plan["needed_orders"][0]["order_number"], "111-3333333-3333333")

            # Seed cached order and run sync with fake categorizer
            (data_dir / "orders").mkdir(parents=True)
            (data_dir / "orders" / "111-3333333-3333333.json").write_text(
                json.dumps(
                    {
                        "order_number": "111-3333333-3333333",
                        "grand_total": 100.0,
                        "items": [
                            {"title": "Organic Oats", "price": 40.0, "qty": 1, "shipment": 0},
                            {"title": "Board Game", "price": 60.0, "qty": 1, "shipment": 0},
                        ],
                    }
                )
            )
            cat = FakeCategorizer({"Organic Oats": "Groceries", "Board Game": "Kids"})
            res = handle_amazon_sync({"charges": charges, "days": 365, "dry_run": False}, sure, cat, data_dir)
            self.assertEqual(res["stats"]["split"], 1)
            self.assertEqual(len(sure.splits), 1)
            self.assertEqual(len(sure.splits[0][1]), 2)
            # Re-running should skip already handled transaction
            res2 = handle_amazon_sync({"charges": charges, "days": 365, "dry_run": False}, sure, cat, data_dir)
            self.assertEqual(res2["stats"]["skipped"], 1)

    def test_spending_categories_exclude_and_include_parents(self):
        from server.sure_client import SureClient

        client = SureClient("http://example", "key")
        sample = [
            {"id": "1", "name": "Shopping", "subcategories_count": 0, "parent": None},
            {"id": "2", "name": "Fun Money", "subcategories_count": 2, "parent": None},
            {"id": "3", "name": "Adam Fun Money", "subcategories_count": 0, "parent": {"id": "2"}},
            {"id": "4", "name": "Aanika Fun Money", "subcategories_count": 0, "parent": {"id": "2"}},
            {"id": "5", "name": "Food & Drink", "subcategories_count": 1, "parent": None},
            {"id": "6", "name": "Eating Out", "subcategories_count": 0, "parent": {"id": "5"}},
        ]
        with patch.object(client, "paged", return_value=iter(sample)):
            cats = client.spending_categories(
                exclude_names={"Adam Fun Money", "Aanika Fun Money"},
                include_parents={"Fun Money"},
            )
        self.assertEqual(set(cats.keys()), {"Shopping", "Fun Money", "Eating Out"})

    def test_jev_categorizer_passes_category_hints(self):
        from server.categorizer import JevCategorizer

        cat = JevCategorizer(
            api_key="test-key",
            category_hints={"Fun Money": "Hobbies, crafts, treats"},
        )
        captured = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"answers": {"item_0": {"choice": "Fun Money", "confidence": 0.92}}}

        def fake_post(url, timeout, headers, json):
            captured["payload"] = json
            return FakeResp()

        with patch("server.categorizer.requests.post", side_effect=fake_post):
            res = cat.categorize(
                "Amazon",
                [{"title": "Bamboo Knitting Needles", "price": 12.52}],
                {"Shopping": "1", "Fun Money": "2"},
            )
        self.assertEqual(res, ["Fun Money"])
        self.assertEqual(
            captured["payload"]["questions"]["item_0"]["criteria"]["Fun Money"],
            "Hobbies, crafts, treats",
        )
        self.assertIsNone(captured["payload"]["questions"]["item_0"]["criteria"]["Shopping"])

    def test_http_auth_and_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = make_handler("super-secret-token", "http://127.0.0.1:1", "key", Path(tmp))
            srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                r = requests.get(f"http://127.0.0.1:{port}/health", timeout=5)
                self.assertEqual(r.status_code, 200)
                self.assertTrue(r.json()["ok"])

                r401 = requests.get(f"http://127.0.0.1:{port}/api/status", timeout=5)
                self.assertEqual(r401.status_code, 401)

                rbad = requests.get(
                    f"http://127.0.0.1:{port}/api/status",
                    headers={"Authorization": "Bearer wrong"},
                    timeout=5,
                )
                self.assertEqual(rbad.status_code, 401)

                with patch("server.app.SureClient.spending_categories", return_value={"Shopping": "1"}):
                    rok = requests.get(
                        f"http://127.0.0.1:{port}/api/status",
                        headers={"Authorization": "Bearer super-secret-token"},
                        timeout=5,
                    )
                self.assertEqual(rok.status_code, 200)
                self.assertTrue(rok.json()["sure_connected"])
                self.assertEqual(rok.json()["spending_categories"], 1)
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
