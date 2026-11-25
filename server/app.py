"""HTTP API server for Sure Retail Sync.

Endpoints:
  GET  /health           -> 200 {"ok": true} (unauthenticated, for Docker healthcheck)
  GET  /api/status       -> 200 server capabilities, Sure connectivity, cache counts
  POST /api/amazon/plan  -> Parse Transactions page(s) HTML/JSON, match against Sure,
                            return missing order IDs/URLs + pagination instruction
  POST /api/amazon/sync  -> Parse supplied Order Details HTML, categorize items, update/split
                            transactions in Sure, and return detailed summary
"""

import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .amazon_parser import (
    DEFAULT_DATE_WINDOW_DAYS,
    DEFAULT_MIN_SPLIT_SHARE,
    NOTE_MARK,
    allocate_splits,
    format_notes,
    items_for_charge,
    match_charges_to_sure,
    parse_order_html,
    parse_transactions_html,
    parse_transactions_json,
)
from .categorizer import build_categorizer
from .paypal_parser import (
    NOTE_MARK as PAYPAL_NOTE_MARK,
    format_paypal_notes,
    is_generic_paypal_name,
    match_paypal_to_sure,
    merge_paypal_detail,
    parse_paypal_filter_response,
)
from .sure_client import SureClient

log = logging.getLogger("sure-retail-sync")

MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(25 * 1024 * 1024)))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATE_WINDOW_DAYS = int(os.environ.get("DATE_WINDOW_DAYS", str(DEFAULT_DATE_WINDOW_DAYS)))
MIN_SPLIT_SHARE = float(os.environ.get("MIN_SPLIT_SHARE", str(DEFAULT_MIN_SPLIT_SHARE)))


def _csv_env(name: str) -> set[str]:
    return {item.strip() for item in os.environ.get(name, "").split(",") if item.strip()}


def _load_spending_categories(sure: SureClient) -> dict[str, str]:
    return sure.spending_categories(
        exclude_names=_csv_env("EXCLUDE_CATEGORIES"),
        include_parents=_csv_env("INCLUDE_PARENT_CATEGORIES"),
    )


def _load_state(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "state.json"
    if path.exists():
        return json.loads(path.read_text())
    return {"done": {}}


def _save_state(data_dir: Path, state: dict[str, Any]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "state.json"
    tmp = data_dir / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def _order_cache_path(data_dir: Path, order_number: str) -> Path:
    safe = "".join(ch for ch in order_number if ch.isalnum() or ch in "-_")
    return data_dir / "orders" / f"{safe}.json"


def _paypal_cache_path(data_dir: Path, tx_id: str) -> Path:
    safe = "".join(ch for ch in tx_id if ch.isalnum() or ch in "-_")
    return data_dir / "paypal" / f"{safe}.json"


def _is_already_handled(txn: dict[str, Any], state: dict[str, Any], note_mark: str = NOTE_MARK) -> bool:
    if txn["id"] in state["done"]:
        return True
    if txn.get("user_modified") or txn.get("split_parent"):
        return True
    if note_mark in (txn.get("notes") or ""):
        return True
    return False


def _dedupe_charges(charges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for c in charges:
        key = (c.get("completed_date"), c.get("cents"), c.get("order_number"), c.get("payment_method_last_4"))
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _extract_charges(body: dict[str, Any], days: int | None) -> tuple[list[dict[str, Any]], dict | None]:
    charges: list[dict[str, Any]] = []
    next_page = None

    if body.get("transactions_html"):
        htmls = body["transactions_html"]
        if isinstance(htmls, str):
            htmls = [htmls]
        for html in htmls:
            parsed, next_page = parse_transactions_html(html)
            charges.extend(parsed)

    for page_obj in body.get("transactions_json_pages") or []:
        parsed, next_page = parse_transactions_json(page_obj["payload"], page_obj.get("meta") or {})
        charges.extend(parsed)

    if body.get("charges"):
        charges.extend(body["charges"])

    charges = _dedupe_charges(charges)
    if days is not None:
        cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        oldest_in_batch = min((c["completed_date"] for c in charges), default=None)
        charges = [c for c in charges if c["completed_date"] >= cutoff]
        if oldest_in_batch and oldest_in_batch < cutoff:
            next_page = None

    return charges, next_page


def handle_amazon_plan(body: dict[str, Any], sure: SureClient, data_dir: Path) -> dict[str, Any]:
    days = int(body.get("days", 45))
    charges, next_page = _extract_charges(body, days=days)
    actionable = [c for c in charges if c.get("order_number")]
    if not actionable:
        return {
            "charges_parsed": len(charges),
            "matched": 0,
            "skipped": 0,
            "unmatched": 0,
            "needed_orders": [],
            "cached_orders": [],
            "next_page": next_page,
        }

    dates = [dt.date.fromisoformat(c["completed_date"]) for c in actionable]
    start = min(dates) - dt.timedelta(days=DATE_WINDOW_DAYS)
    end = max(dates) + dt.timedelta(days=DATE_WINDOW_DAYS)
    candidates = sure.amazon_transactions(start, end)
    state = _load_state(data_dir)

    matched_pairs = match_charges_to_sure(actionable, candidates, date_window_days=DATE_WINDOW_DAYS)
    needed: dict[str, str] = {}
    cached: set[str] = set()
    matched = skipped = unmatched = 0

    for c, txn in matched_pairs:
        if txn is None:
            unmatched += 1
            continue
        if _is_already_handled(txn, state):
            skipped += 1
            continue
        matched += 1
        oid = c["order_number"]
        if _order_cache_path(data_dir, oid).exists():
            cached.add(oid)
        else:
            url = c.get("order_details_link") or f"https://www.amazon.com/gp/your-account/order-details?orderID={oid}"
            if url.startswith("/"):
                url = f"https://www.amazon.com{url}"
            needed[oid] = url

    return {
        "charges_parsed": len(charges),
        "charges": actionable,
        "matched": matched,
        "skipped": skipped,
        "unmatched": unmatched,
        "needed_orders": [{"order_number": k, "url": v} for k, v in needed.items()],
        "cached_orders": sorted(cached),
        "next_page": next_page,
    }


def handle_amazon_sync(body: dict[str, Any], sure: SureClient, categorizer, data_dir: Path) -> dict[str, Any]:
    days = int(body.get("days", 45))
    dry_run = bool(body.get("dry_run", False))
    sure.dry_run = dry_run

    charges, _ = _extract_charges(body, days=days)
    actionable = [c for c in charges if c.get("order_number")]

    # Parse & cache any newly supplied Order Details HTML pages
    orders_dir = data_dir / "orders"
    orders_dir.mkdir(parents=True, exist_ok=True)
    parse_errors: list[dict[str, str]] = []
    for oid, html in (body.get("orders_html") or {}).items():
        try:
            order_data = parse_order_html(html, order_number=oid)
            if not dry_run or order_data["items"]:
                _order_cache_path(data_dir, oid).write_text(json.dumps(order_data, indent=2))
        except Exception as exc:
            log.warning("Failed to parse order %s: %s", oid, exc)
            parse_errors.append({"order_number": oid, "error": str(exc)})

    if not actionable:
        return {
            "dry_run": dry_run,
            "categorizer": categorizer.name,
            "stats": {"updated": 0, "split": 0, "skipped": 0, "unmatched": 0, "missing_order": 0},
            "changes": [],
            "parse_errors": parse_errors,
        }

    dates = [dt.date.fromisoformat(c["completed_date"]) for c in actionable]
    start = min(dates) - dt.timedelta(days=DATE_WINDOW_DAYS)
    end = max(dates) + dt.timedelta(days=DATE_WINDOW_DAYS)
    candidates = sure.amazon_transactions(start, end)
    categories = _load_spending_categories(sure)
    state = _load_state(data_dir)

    stats = {"updated": 0, "split": 0, "skipped": 0, "unmatched": 0, "missing_order": 0}
    changes: list[dict[str, Any]] = []

    for c, txn in match_charges_to_sure(actionable, candidates, date_window_days=DATE_WINDOW_DAYS):
        if txn is None:
            stats["unmatched"] += 1
            continue
        if _is_already_handled(txn, state):
            stats["skipped"] += 1
            continue

        oid = c["order_number"]
        cache_path = _order_cache_path(data_dir, oid)
        if not cache_path.exists():
            stats["missing_order"] += 1
            continue

        order = json.loads(cache_path.read_text())
        items = items_for_charge(order, abs(c["grand_total"]))
        if not items:
            stats["missing_order"] += 1
            continue

        note = format_notes(oid, items)
        raw_cats = categorizer.categorize("Amazon", items, categories) if categories else [None] * len(items)
        resolved_cats = [cat for cat in raw_cats if cat in categories]

        # Notes-only mode (categorizer == none or no valid category)
        if len(resolved_cats) != len(items):
            sure.update_transaction(txn["id"], notes=note)
            stats["updated"] += 1
            changes.append({
                "action": "notes_only",
                "date": txn["date"],
                "account": txn["account"]["name"],
                "amount": abs(c["grand_total"]),
                "order_number": oid,
                "category": None,
                "notes": note,
            })
        else:
            shares = allocate_splits(abs(c["cents"]), items, raw_cats, min_split_share=MIN_SPLIT_SHARE)
            if len(shares) == 1 or c["is_refund"]:
                cat = max(shares, key=shares.get)
                sure.update_transaction(txn["id"], category_id=categories[cat], notes=note)
                stats["updated"] += 1
                changes.append({
                    "action": "updated",
                    "date": txn["date"],
                    "account": txn["account"]["name"],
                    "amount": abs(c["grand_total"]),
                    "order_number": oid,
                    "category": cat,
                    "notes": note,
                })
            else:
                sure.update_transaction(txn["id"], notes=note)
                sure.split_transaction(
                    txn["id"],
                    [
                        {
                            "name": f"Amazon - {cat}",
                            "amount": f"{cents / 100:.2f}",
                            "category_id": categories[cat],
                        }
                        for cat, cents in shares.items()
                    ],
                )
                stats["split"] += 1
                changes.append({
                    "action": "split",
                    "date": txn["date"],
                    "account": txn["account"]["name"],
                    "amount": abs(c["grand_total"]),
                    "order_number": oid,
                    "splits": {cat: round(cents / 100, 2) for cat, cents in shares.items()},
                    "notes": note,
                })

        if not dry_run:
            state["done"][txn["id"]] = {
                "order": oid,
                "at": dt.datetime.now().isoformat(timespec="seconds"),
            }
            _save_state(data_dir, state)

    log.info("sync%s: %s", " (DRY RUN)" if dry_run else "", stats)
    return {
        "dry_run": dry_run,
        "categorizer": categorizer.name,
        "stats": stats,
        "changes": changes,
        "parse_errors": parse_errors,
    }


def _extract_paypal_charges(body: dict[str, Any], days: int | None) -> tuple[list[dict[str, Any]], str | None]:
    charges: list[dict[str, Any]] = []
    next_token = None

    for page in body.get("filter_pages") or []:
        parsed, next_token = parse_paypal_filter_response(page)
        if not parsed and isinstance(page, dict):
            log.info("PayPal filter page parsed 0 items; top keys=%s", list(page.keys())[:10])
        charges.extend(parsed)

    if body.get("charges"):
        charges.extend(body["charges"])

    seen = set()
    deduped = []
    for c in charges:
        key = c.get("id") or (c.get("completed_date"), c.get("cents"), c.get("merchant"))
        if key not in seen:
            seen.add(key)
            deduped.append(c)

    if days is not None:
        cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        oldest = min((c["completed_date"] for c in deduped), default=None)
        deduped = [c for c in deduped if c["completed_date"] >= cutoff]
        if oldest and oldest < cutoff:
            next_token = None

    return deduped, next_token


def handle_paypal_plan(body: dict[str, Any], sure: SureClient, data_dir: Path) -> dict[str, Any]:
    days = int(body.get("days", 45))
    charges, next_token = _extract_paypal_charges(body, days=days)
    if not charges:
        return {
            "charges_parsed": 0,
            "charges": [],
            "matched": 0,
            "skipped": 0,
            "unmatched": 0,
            "needed_details": [],
            "cached_details": [],
            "next_page_token": next_token,
        }

    dates = [dt.date.fromisoformat(c["completed_date"]) for c in charges]
    start = min(dates) - dt.timedelta(days=DATE_WINDOW_DAYS)
    end = max(dates) + dt.timedelta(days=DATE_WINDOW_DAYS)
    candidates = sure.paypal_transactions(start, end)
    state = _load_state(data_dir)

    needed: dict[str, str] = {}
    cached: set[str] = set()
    matched = skipped = unmatched = 0

    for c, txn in match_paypal_to_sure(charges, candidates, date_window_days=DATE_WINDOW_DAYS):
        if txn is None:
            unmatched += 1
            continue
        if _is_already_handled(txn, state, note_mark=PAYPAL_NOTE_MARK):
            skipped += 1
            continue
        matched += 1
        tid = c["id"]
        if _paypal_cache_path(data_dir, tid).exists():
            cached.add(tid)
        else:
            needed[tid] = f"https://www.paypal.com/myaccount/transactions/details/inline/{tid}"

    return {
        "charges_parsed": len(charges),
        "charges": charges,
        "matched": matched,
        "skipped": skipped,
        "unmatched": unmatched,
        "needed_details": [{"id": k, "url": v} for k, v in needed.items()],
        "cached_details": sorted(cached),
        "next_page_token": next_token,
    }


def handle_paypal_sync(body: dict[str, Any], sure: SureClient, categorizer, data_dir: Path) -> dict[str, Any]:
    days = int(body.get("days", 45))
    dry_run = bool(body.get("dry_run", False))
    sure.dry_run = dry_run

    charges, _ = _extract_paypal_charges(body, days=days)
    paypal_dir = data_dir / "paypal"
    paypal_dir.mkdir(parents=True, exist_ok=True)

    # Cache newly fetched detail payloads
    details_by_id = body.get("details_json") or {}
    for tid, payload in details_by_id.items():
        if isinstance(payload, dict):
            _paypal_cache_path(data_dir, tid).write_text(json.dumps(payload, indent=2))

    if not charges:
        return {
            "dry_run": dry_run,
            "categorizer": categorizer.name,
            "stats": {"updated": 0, "split": 0, "skipped": 0, "unmatched": 0},
            "changes": [],
        }

    dates = [dt.date.fromisoformat(c["completed_date"]) for c in charges]
    start = min(dates) - dt.timedelta(days=DATE_WINDOW_DAYS)
    end = max(dates) + dt.timedelta(days=DATE_WINDOW_DAYS)
    candidates = sure.paypal_transactions(start, end)
    categories = _load_spending_categories(sure)
    state = _load_state(data_dir)

    stats = {"updated": 0, "split": 0, "skipped": 0, "unmatched": 0}
    changes: list[dict[str, Any]] = []

    for c, txn in match_paypal_to_sure(charges, candidates, date_window_days=DATE_WINDOW_DAYS):
        if txn is None:
            stats["unmatched"] += 1
            continue
        if _is_already_handled(txn, state, note_mark=PAYPAL_NOTE_MARK):
            stats["skipped"] += 1
            continue

        tx = dict(c)
        cache_path = _paypal_cache_path(data_dir, tx["id"])
        if cache_path.exists():
            try:
                tx = merge_paypal_detail(tx, json.loads(cache_path.read_text()))
            except Exception as exc:
                log.warning("Failed to merge PayPal cache %s: %s", tx["id"], exc)

        merchant = tx.get("merchant") or ""
        if not merchant and not tx.get("description"):
            stats["unmatched"] += 1
            continue

        note = format_paypal_notes(tx)
        item_title = f"{merchant} — {tx['description']}" if tx.get("description") and merchant else (merchant or tx["description"])
        raw_cats = (
            categorizer.categorize("PayPal", [{"title": item_title, "price": abs(tx["grand_total"])}], categories)
            if categories
            else [None]
        )
        chosen_cat = raw_cats[0] if raw_cats and raw_cats[0] in categories else None

        fields: dict[str, Any] = {"notes": note}
        if chosen_cat:
            fields["category_id"] = categories[chosen_cat]
        if merchant and is_generic_paypal_name(txn.get("name") or ""):
            fields["name"] = f"{merchant} (PayPal)"

        sure.update_transaction(txn["id"], **fields)
        stats["updated"] += 1
        changes.append({
            "action": "updated" if chosen_cat else "notes_only",
            "date": txn["date"],
            "account": txn["account"]["name"],
            "amount": abs(tx["grand_total"]),
            "merchant": merchant,
            "category": chosen_cat,
            "notes": note,
            "renamed_to": fields.get("name"),
        })

        if not dry_run:
            state["done"][txn["id"]] = {
                "source": "paypal",
                "paypal_id": tx["id"],
                "at": dt.datetime.now().isoformat(timespec="seconds"),
            }
            _save_state(data_dir, state)

    log.info("paypal sync%s: %s", " (DRY RUN)" if dry_run else "", stats)
    return {
        "dry_run": dry_run,
        "categorizer": categorizer.name,
        "stats": stats,
        "changes": changes,
    }


def _check_bearer(auth_header: str, expected_token: str) -> bool:
    if not expected_token:
        return False
    parts = (auth_header or "").strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    a = hashlib.sha256(parts[1].strip().encode()).digest()
    b = hashlib.sha256(expected_token.encode()).digest()
    return hmac.compare_digest(a, b)


def make_handler(
    sync_token: str,
    sure_url: str,
    sure_api_key: str,
    data_dir: Path = DATA_DIR,
):
    class SyncHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.info("%s - %s", self.address_string(), fmt % args)

        def _send_cors_headers(self):
            origin = self.headers.get("Origin") or "*"
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Vary", "Origin")

        def _send_json(self, status: int, payload: dict[str, Any]):
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _require_auth(self) -> bool:
            if not _check_bearer(self.headers.get("Authorization", ""), sync_token):
                self._send_json(401, {"error": "unauthorized"})
                return False
            return True

        def _read_json_body(self) -> dict[str, Any] | None:
            length = int(self.headers.get("Content-Length", "0"))
            if length > MAX_BODY_BYTES:
                self._send_json(413, {"error": "payload_too_large"})
                return None
            raw = self.rfile.read(length) if length else b"{}"
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": f"invalid_json: {exc}"})
                return None

        def do_OPTIONS(self):
            self.send_response(204)
            self._send_cors_headers()
            self.end_headers()

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/health":
                return self._send_json(200, {"ok": True})
            if path == "/api/status":
                if not self._require_auth():
                    return
                state = _load_state(data_dir)
                cached_count = len(list((data_dir / "orders").glob("*.json"))) if (data_dir / "orders").exists() else 0
                sure_ok = False
                cat_count = 0
                sure_err = None
                try:
                    cats = _load_spending_categories(SureClient(sure_url, sure_api_key))
                    sure_ok = True
                    cat_count = len(cats)
                except Exception as exc:
                    sure_err = str(exc)
                return self._send_json(
                    200,
                    {
                        "ok": sure_ok,
                        "sure_connected": sure_ok,
                        "sure_error": sure_err,
                        "spending_categories": cat_count,
                        "categorizer": build_categorizer().name,
                        "synced_transactions": len(state.get("done", {})),
                        "cached_orders": cached_count,
                        "sources": ["amazon", "paypal"],
                    },
                )
            return self._send_json(404, {"error": "not_found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            if path not in ("/api/amazon/plan", "/api/amazon/sync", "/api/paypal/plan", "/api/paypal/sync"):
                return self._send_json(404, {"error": "not_found"})
            if not self._require_auth():
                return
            body = self._read_json_body()
            if body is None:
                return
            try:
                sure = SureClient(sure_url, sure_api_key)
                if path == "/api/amazon/plan":
                    result = handle_amazon_plan(body, sure, data_dir)
                elif path == "/api/amazon/sync":
                    result = handle_amazon_sync(body, sure, build_categorizer(), data_dir)
                elif path == "/api/paypal/plan":
                    result = handle_paypal_plan(body, sure, data_dir)
                else:
                    result = handle_paypal_sync(body, sure, build_categorizer(), data_dir)
                return self._send_json(200, result)
            except Exception as exc:
                log.exception("Request %s failed", path)
                return self._send_json(500, {"error": str(exc)})

    return SyncHandler


def main():
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    sync_token = os.environ.get("SYNC_API_TOKEN", "").strip()
    sure_url = os.environ.get("SURE_URL", "http://sure:3000").strip()
    sure_api_key = os.environ.get("SURE_API_KEY", "").strip()
    port = int(os.environ.get("PORT", "8080"))

    if not sync_token:
        log.error("SYNC_API_TOKEN must be set")
        sys.exit(1)
    if not sure_api_key:
        log.error("SURE_API_KEY must be set")
        sys.exit(1)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = make_handler(sync_token, sure_url, sure_api_key, DATA_DIR)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    log.info(
        "sure-retail-sync listening on :%d (sure=%s, categorizer=%s)",
        port,
        sure_url,
        build_categorizer().name,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
