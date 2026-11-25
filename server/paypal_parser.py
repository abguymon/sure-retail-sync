"""PayPal web activity JSON parsing and Sure transaction matching.

Parses JSON responses captured by the browser extension from PayPal's signed-in
web activity endpoints (/myaccount/transactions/filter and /details/inline/<id>)
without ever storing PayPal credentials on the server.
"""

import datetime as dt
import re
from typing import Any

NOTE_MARK = "[paypal]"
DEFAULT_DATE_WINDOW_DAYS = 7
GENERIC_PAYPAL_NAME_RE = re.compile(
    r"^\s*(paypal(\s*-\s*)?)?paypal\s+(purchase|inst\s*xfer|transfer|payment|debit).*$",
    re.I,
)


def _walk_for_transactions(obj: Any) -> list[dict[str, Any]]:
    """Locate the list of PayPal transaction dicts across known response shapes."""
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if not isinstance(obj, dict):
        return []
    # Common paths in PayPal's /myaccount/transactions/filter responses
    for path in (
        ("data", "data", "activity", "transactions"),
        ("data", "activity", "transactions"),
        ("activity", "transactions"),
        ("transactions",),
        ("data", "transactions"),
    ):
        cur: Any = obj
        ok = True
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                ok = False
                break
        if ok and isinstance(cur, list):
            return [x for x in cur if isinstance(x, dict)]
    return []


def _extract_next_page_token(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    for path in (
        ("data", "data", "activity", "nextPageToken"),
        ("data", "activity", "nextPageToken"),
        ("activity", "nextPageToken"),
        ("nextPageToken",),
    ):
        cur: Any = obj
        for key in path:
            cur = cur.get(key) if isinstance(cur, dict) else None
        if isinstance(cur, str) and cur.strip():
            return cur.strip()
    return None


def _parse_date(raw: Any) -> str | None:
    if not raw:
        return None
    if isinstance(raw, (int, float)):
        ts = raw / 1000.0 if raw > 1e12 else float(raw)
        return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).date().isoformat()
    s = str(raw).strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%d %b %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_amount_float(raw: Any, is_credit: bool | None = None) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        for k in ("amount", "value", "unformatted", "formatted", "amountFormatted"):
            if k in raw and raw[k] is not None:
                val = _parse_amount_float(raw[k], is_credit=is_credit)
                if val is not None:
                    return val
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
    else:
        s = str(raw).strip()
        neg = s.startswith("-") or s.startswith("(") or "−" in s
        pos = s.startswith("+")
        cleaned = re.sub(r"[^0-9.]", "", s)
        if not cleaned:
            return None
        val = float(cleaned)
        if neg:
            val = -abs(val)
        elif pos:
            val = abs(val)
    if is_credit is True:
        return abs(val)
    if is_credit is False:
        return -abs(val)
    return val


def _extract_last4(tx: dict[str, Any]) -> str | None:
    for key in ("fundingSource", "paymentMethod", "funding", "fundingSources"):
        val = tx.get(key)
        if not val:
            continue
        m = re.search(r"(?:x{2,4}|\*{2,4}|ending\s+in\s+|•{2,4}|-)(\d{4})\b", str(val), re.I)
        if m:
            return m.group(1)
    return None


def _extract_merchant(tx: dict[str, Any]) -> str:
    for key in ("displayName", "counterparty", "merchantName", "name", "title", "payeeName"):
        val = tx.get(key)
        if isinstance(val, dict):
            val = val.get("name") or val.get("businessName") or val.get("displayName") or val.get("email")
        if isinstance(val, str) and val.strip():
            return re.sub(r"\s+", " ", val).strip()
    return ""


def _extract_description(tx: dict[str, Any]) -> str:
    parts: list[str] = []
    # Inline detail item lists
    for list_key in ("items", "cartItems", "itemList"):
        items = tx.get(list_key)
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    t = it.get("name") or it.get("title") or it.get("description")
                    if t:
                        qty = it.get("quantity") or 1
                        s = re.sub(r"\s+", " ", str(t)).strip()
                        parts.append(f"{s} (x{qty})" if str(qty) not in ("1", "1.0", "") else s)
    for key in ("itemTitle", "itemName", "customNote", "note", "memo", "description", "type", "txnType"):
        val = tx.get(key)
        if isinstance(val, str) and val.strip():
            s = re.sub(r"\s+", " ", val).strip()
            if s.lower() not in ("payment", "purchase", "preapproved payment", "automatic payment") and s not in parts:
                parts.append(s)
    return "; ".join(parts[:5])


def normalize_paypal_transaction(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a raw PayPal activity dict into our standard charge structure."""
    tx_id = str(raw.get("id") or raw.get("transactionId") or raw.get("encryptedTransactionId") or "").strip()
    if not tx_id:
        return None

    date_str = None
    for dk in ("timeCreated", "date", "transactionDate", "formattedDate", "displayDate", "createTime"):
        date_str = _parse_date(raw.get(dk))
        if date_str:
            break
    if not date_str:
        return None

    is_credit = raw.get("isCredit")
    if not isinstance(is_credit, bool):
        direction = str(raw.get("direction") or raw.get("creditDebit") or "").lower()
        if direction in ("credit", "in", "cr"):
            is_credit = True
        elif direction in ("debit", "out", "dr"):
            is_credit = False
        else:
            is_credit = None

    amount = None
    for ak in ("grossAmount", "netAmount", "amount", "formattedAmount", "totalAmount", "displayAmount"):
        amount = _parse_amount_float(raw.get(ak), is_credit=is_credit)
        if amount is not None:
            break
    if amount is None or abs(amount) < 0.005:
        return None

    # Default unsigned purchases to negative (outflow) unless clearly a credit/refund
    if is_credit is None and amount > 0:
        t_str = str(raw.get("type") or raw.get("transactionType") or raw.get("status") or "").lower()
        if not any(w in t_str for w in ("refund", "received", "credit", "deposit", "cashback")):
            amount = -abs(amount)

    merchant = _extract_merchant(raw)
    description = _extract_description(raw)
    status = str(raw.get("status") or raw.get("transactionStatus") or "").strip()

    return {
        "id": tx_id,
        "completed_date": date_str,
        "grand_total": round(amount, 2),
        "cents": round(amount * 100),
        "is_refund": amount > 0,
        "merchant": merchant,
        "description": description,
        "status": status,
        "payment_method_last_4": _extract_last4(raw),
    }


def parse_paypal_filter_response(payload: Any) -> tuple[list[dict[str, Any]], str | None]:
    """Parse one /myaccount/transactions/filter JSON response."""
    raw_list = _walk_for_transactions(payload)
    out = []
    for raw in raw_list:
        norm = normalize_paypal_transaction(raw)
        if norm:
            out.append(norm)
    return out, _extract_next_page_token(payload)


def merge_paypal_detail(base: dict[str, Any], detail_payload: Any) -> dict[str, Any]:
    """Enrich a normalized PayPal transaction with fields from /details/inline/<id>."""
    if not isinstance(detail_payload, dict):
        return base
    # Drill into PayPal's nested detail wrapper if present
    inner = detail_payload
    for path in (("data", "data", "details"), ("data", "details"), ("details",), ("data",)):
        cur: Any = detail_payload
        for key in path:
            cur = cur.get(key) if isinstance(cur, dict) else None
        if isinstance(cur, dict):
            inner = cur
            break

    merged = dict(base)
    if not merged.get("merchant"):
        merged["merchant"] = _extract_merchant(inner)
    extra_desc = _extract_description(inner)
    if extra_desc and extra_desc not in (merged.get("description") or ""):
        merged["description"] = (
            f"{merged['description']}; {extra_desc}" if merged.get("description") else extra_desc
        )
    if not merged.get("payment_method_last_4"):
        merged["payment_method_last_4"] = _extract_last4(inner)
    return merged


def is_generic_paypal_name(name: str) -> bool:
    return bool(GENERIC_PAYPAL_NAME_RE.match((name or "").strip()))


def format_paypal_notes(tx: dict[str, Any]) -> str:
    merchant = tx.get("merchant") or "PayPal"
    desc = (tx.get("description") or "").strip()
    if desc and desc.lower() != merchant.lower():
        text = f"{merchant}: {desc}"
    else:
        text = merchant
    if len(text) > 180:
        text = text[:177] + "..."
    return f"{NOTE_MARK} {text} (ID {tx['id']})"


def match_paypal_to_sure(
    charges: list[dict[str, Any]],
    sure_txns: list[dict[str, Any]],
    date_window_days: int = DEFAULT_DATE_WINDOW_DAYS,
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Match PayPal charges to Sure PayPal transactions by exact cents, date gap, and optional card last-4."""
    claimed: set[str] = set()
    results: list[tuple[dict[str, Any], dict[str, Any] | None]] = []

    for c in sorted(charges, key=lambda x: x["completed_date"]):
        cents = c["cents"]
        c_date = dt.date.fromisoformat(c["completed_date"])
        last4 = c.get("payment_method_last_4")
        merchant_low = (c.get("merchant") or "").lower()
        best: tuple[int, int, int, dict[str, Any]] | None = None

        for t in sure_txns:
            if t["id"] in claimed:
                continue
            if t.get("signed_amount_cents") != cents:
                continue
            t_date = dt.date.fromisoformat(t["date"])
            gap = abs((t_date - c_date).days)
            if gap > date_window_days:
                continue
            acc_name = (t.get("account") or {}).get("name", "")
            last4_match = bool(last4 and f"({last4})" in acc_name)
            t_name_low = (t.get("name") or "").lower()
            # Prefer exact last-4 match, then merchant-name match in Sure's PAYPAL *MERCHANT, then smallest date gap
            merch_hit = bool(merchant_low and len(merchant_low) >= 4 and merchant_low[:6] in t_name_low)
            score = (0 if last4_match else 1, 0 if merch_hit else 1, gap)
            if best is None or score < (best[0], best[1], best[2]):
                best = (score[0], score[1], score[2], t)

        if best is not None:
            claimed.add(best[3]["id"])
            results.append((c, best[3]))
        else:
            results.append((c, None))

    return results
