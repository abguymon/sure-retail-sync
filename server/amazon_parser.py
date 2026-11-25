"""Amazon HTML/JSON parsing, charge-to-order matching, and split allocation.

Uses amazon-orders' built-in network-free parsers so the Chrome extension never
needs to duplicate Amazon's HTML selectors in JavaScript.
"""

import datetime as dt
import re
from typing import Any

from amazonorders.conf import AmazonOrdersConfig
from amazonorders.orders import AmazonOrders
from bs4 import BeautifulSoup

NOTE_MARK = "[amazon]"
DEFAULT_DATE_WINDOW_DAYS = 7
DEFAULT_MIN_SPLIT_SHARE = 0.10


def _amazon_config() -> AmazonOrdersConfig:
    return AmazonOrdersConfig(data={"warn_on_missing_required_field": True})


def _tx_to_dict(tx) -> dict[str, Any]:
    return {
        "completed_date": tx.completed_date.isoformat(),
        "grand_total": float(tx.grand_total),
        "cents": round(float(tx.grand_total) * 100),
        "is_refund": float(tx.grand_total) > 0,
        "order_number": tx.order_number,
        "order_details_link": tx.order_details_link,
        "payment_method": tx.payment_method,
        "payment_method_last_4": tx.payment_method_last_4,
        "seller": tx.seller,
    }


def _next_page_instruction(next_page_data: dict | None, cfg: AmazonOrdersConfig) -> dict | None:
    if not next_page_data:
        return None
    if "exclusiveStartKey" in next_page_data:
        return {
            "kind": "json",
            "url": cfg.constants.TRANSACTION_DATA_URL,
            "body": {
                "type": "GetTransactions",
                "locale": next_page_data["locale"],
                "exclusiveStartKey": next_page_data["exclusiveStartKey"],
                "widgetName": next_page_data["widgetName"],
            },
            "headers": {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "x-amzn-upx-token": next_page_data["token"],
            },
            "meta": next_page_data,
        }
    return {
        "kind": "form",
        "url": cfg.constants.TRANSACTION_HISTORY_URL,
        "form_data": next_page_data,
    }


def parse_transactions_html(html: str) -> tuple[list[dict[str, Any]], dict | None]:
    """Parse an Amazon Transactions HTML page; returns (charges, next_page_instruction)."""
    cfg = _amazon_config()
    parsed = BeautifulSoup(html, cfg.bs4_parser)
    page = cfg.transactions_page_cls(cfg)
    txs, next_page_data = page.parse_page(parsed)
    return [_tx_to_dict(tx) for tx in txs], _next_page_instruction(next_page_data, cfg)


def parse_transactions_json(payload: dict[str, Any], prev_meta: dict[str, Any]) -> tuple[list[dict[str, Any]], dict | None]:
    """Parse a paginated JSON response from Amazon's get-transactions endpoint."""
    cfg = _amazon_config()
    page = cfg.transactions_page_cls(cfg)
    display = payload.get("displayResponse") or {}
    tx_list = display.get("transactionsList") or []
    txs = page._parse_transaction_list(tx_list)
    last_key = display.get("lastEvaluatedKey")
    next_meta = {**prev_meta, "exclusiveStartKey": last_key} if last_key and txs else None
    return [_tx_to_dict(tx) for tx in txs], _next_page_instruction(next_meta, cfg)


def parse_order_html(html: str, order_number: str) -> dict[str, Any]:
    """Parse an Amazon Order Details HTML page into a normalized order dict."""
    cfg = _amazon_config()
    order = AmazonOrders.parse_order(html, cfg, order_number=order_number)
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, float, int]] = set()
    for si, shipment in enumerate(order.shipments or []):
        for it in shipment.items or []:
            key = (it.title, float(it.price or 0), int(it.quantity or 1))
            items.append({
                "title": it.title,
                "price": float(it.price or 0),
                "qty": int(it.quantity or 1),
                "shipment": si,
            })
            seen.add(key)
    for it in order.items or []:
        key = (it.title, float(it.price or 0), int(it.quantity or 1))
        if key not in seen:
            items.append({
                "title": it.title,
                "price": float(it.price or 0),
                "qty": int(it.quantity or 1),
                "shipment": None,
            })
    return {
        "order_number": order.order_number or order_number,
        "grand_total": float(order.grand_total or 0),
        "items": items,
    }


def items_for_charge(order: dict[str, Any], charge_abs: float) -> list[dict[str, Any]]:
    """Return the items a card charge paid for.

    Handles:
    - Full-order charges (charge ~= grand_total)
    - Gift card / Chase points / promo discounts on single-shipment orders (charge < grand_total)
    - Multi-shipment partial billing (matches the shipment closest to the charge)
    """
    items = order.get("items") or []
    if not items:
        return []
    groups: dict[Any, list[dict[str, Any]]] = {}
    for it in items:
        groups.setdefault(it.get("shipment"), []).append(it)

    # Single shipment (or ungrouped): all items belong to this charge even when
    # gift cards / points / promos reduce the card charge below grand_total.
    if len(groups) <= 1:
        return items

    grand_total = float(order.get("grand_total") or 0)
    if grand_total and abs(charge_abs - grand_total) < 0.02:
        return items

    subtotal = sum(i["price"] * i["qty"] for i in items) or 1.0
    ratio = grand_total / subtotal if grand_total else 1.0
    best = min(groups.values(), key=lambda g: abs(sum(i["price"] * i["qty"] for i in g) * ratio - charge_abs))
    best_total = sum(i["price"] * i["qty"] for i in best) * ratio
    if abs(best_total - charge_abs) <= max(0.50, charge_abs * 0.06):
        return best
    return items


def allocate_splits(
    charge_cents: int,
    items: list[dict[str, Any]],
    cats: list[str],
    min_split_share: float = DEFAULT_MIN_SPLIT_SHARE,
) -> dict[str, int]:
    """Allocate charge_cents across categories proportionally to item value."""
    weight: dict[str, float] = {}
    for it, cat in zip(items, cats):
        weight[cat] = weight.get(cat, 0.0) + max(float(it["price"]) * int(it["qty"]), 0.01)
    total = sum(weight.values())
    biggest = max(weight, key=weight.get)
    for cat in [c for c in weight if c != biggest and (weight[c] / total) < min_split_share]:
        weight[biggest] += weight.pop(cat)
    shares = {c: int(charge_cents * w / total) for c, w in weight.items()}
    shares[biggest] += charge_cents - sum(shares.values())
    return shares


def format_notes(order_number: str, items: list[dict[str, Any]]) -> str:
    titles = []
    for it in items:
        t = re.sub(r"\s+", " ", it["title"]).strip()
        t = t if len(t) <= 80 else t[:77] + "..."
        titles.append(f"{t} (x{it['qty']})" if it["qty"] > 1 else t)
    return f"{NOTE_MARK} Order {order_number}: " + "; ".join(titles)


def match_charges_to_sure(
    charges: list[dict[str, Any]],
    sure_txns: list[dict[str, Any]],
    date_window_days: int = DEFAULT_DATE_WINDOW_DAYS,
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Match Amazon charges to Sure transactions by card last-4, exact cents, and date proximity."""
    claimed: set[str] = set()
    results: list[tuple[dict[str, Any], dict[str, Any] | None]] = []

    for c in sorted(charges, key=lambda x: x["completed_date"]):
        cents = c["cents"]
        c_date = dt.date.fromisoformat(c["completed_date"])
        last4 = c.get("payment_method_last_4")
        best: tuple[int, int, dict[str, Any]] | None = None

        for t in sure_txns:
            if t["id"] in claimed:
                continue
            if t.get("signed_amount_cents") != cents:
                continue
            acc_name = (t.get("account") or {}).get("name", "")
            last4_match = bool(last4 and f"({last4})" in acc_name)
            # If Amazon gave last-4 and Sure account names carry last-4 in parens,
            # require a match when the account name contains any (DDDD) tag.
            has_acc_digits = bool(re.search(r"\(\d{4}\)", acc_name))
            if last4 and has_acc_digits and not last4_match:
                continue
            t_date = dt.date.fromisoformat(t["date"])
            gap = abs((t_date - c_date).days)
            if gap <= date_window_days:
                # Prefer last4_match first, then smallest date gap
                score = (0 if last4_match else 1, gap)
                if best is None or score < (best[0], best[1]):
                    best = (score[0], score[1], t)

        if best is not None:
            claimed.add(best[2]["id"])
            results.append((c, best[2]))
        else:
            results.append((c, None))

    return results
