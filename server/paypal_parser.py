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
        ("data", "data", "activity", "pagination", "nextPageToken"),
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


def _parse_date(raw: Any, fallback_year: int | None = None, _depth: int = 0) -> str | None:
    if not raw or _depth > 4:
        return None
    if isinstance(raw, dict):
        # Nested PayPal activitynodeweb / Activity Design Refresh date dicts
        for k in (
            "isoDate",
            "dateTime",
            "utcTime",
            "timestamp",
            "timeCreated",
            "date",
            "fullDate",
            "formattedDate",
            "displayDate",
            "transactionDateText",
            "dateText",
            "formattedTransactionDate",
            "content",
            "contents",
        ):
            if raw.get(k):
                yr = raw.get("year") or fallback_year
                res = _parse_date(
                    raw[k],
                    fallback_year=int(yr) if str(yr or "").isdigit() else fallback_year,
                    _depth=_depth + 1,
                )
                if res:
                    return res
        yr = raw.get("year") or fallback_year
        mon = raw.get("month")
        day = raw.get("day")
        if yr and mon and day:
            for candidate in (f"{mon} {day}, {yr}", f"{yr}-{mon}-{day}"):
                res = _parse_date(candidate, _depth=_depth + 1)
                if res:
                    return res
        # Fallback: inspect any remaining string/dict values in a date*Info object
        for k, v in raw.items():
            if any(w in k.lower() for w in ("class", "visibility", "icon", "style", "aria")):
                continue
            if isinstance(v, (str, dict, int, float)):
                res = _parse_date(v, fallback_year=fallback_year, _depth=_depth + 1)
                if res:
                    return res
        return None
    if isinstance(raw, (int, float)):
        if raw < 1_000_000_000:
            return None
        ts = raw / 1000.0 if raw > 1e12 else float(raw)
        return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).date().isoformat()
    s = str(raw).strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    # Strip trailing time/bullet suffixes like "Nov 22, 2026 • 12:34 PM" or "Nov 22, 2026"
    s_clean = re.split(r"\s*[•|·]\s*", s)[0].strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%d %b %Y", "%b %d %Y", "%Y-%b-%d"):
        try:
            return dt.datetime.strptime(s_clean, fmt).date().isoformat()
        except ValueError:
            continue
    # Month + day without year (e.g. "Nov 22" or "Oct 7")
    if fallback_year:
        for fmt in ("%b %d %Y", "%B %d %Y", "%d %b %Y"):
            try:
                return dt.datetime.strptime(f"{s_clean} {fallback_year}", fmt).date().isoformat()
            except ValueError:
                continue
    return None


def _parse_amount_float(raw: Any, is_credit: bool | None = None, _depth: int = 0) -> float | None:
    if raw is None or _depth > 4:
        return None
    if isinstance(raw, dict):
        for k in (
            "unformattedAmount",
            "unformatted",
            "amountText",
            "amount",
            "value",
            "gross",
            "net",
            "total",
            "formattedAmount",
            "formatted",
            "formattedAmountWithCurrency",
            "contents",
            "content",
        ):
            if k in raw and raw[k] is not None:
                val = _parse_amount_float(raw[k], is_credit=is_credit, _depth=_depth + 1)
                if val is not None:
                    return val
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
    else:
        s = str(raw).strip()
        neg = s.startswith("-") or s.startswith("(") or "−" in s or "–" in s
        pos = s.startswith("+")
        cleaned = re.sub(r"[^0-9.]", "", s)
        if not cleaned or cleaned.count(".") > 1:
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
    # Inline details: fundingSource.fundingSourceList[].last4
    fs = tx.get("fundingSource")
    if isinstance(fs, dict):
        for entry in fs.get("fundingSourceList") or []:
            if isinstance(entry, dict) and entry.get("last4"):
                digits = re.sub(r"\D", "", str(entry["last4"]))
                if len(digits) == 4:
                    return digits

    for key in ("fundingSource", "paymentMethod", "funding", "fundingSources", "fundingInfo", "paymentInfo"):
        val = tx.get(key)
        if not val:
            continue
        m = re.search(r"(?:x{2,4}|\*{2,4}|ending\s+in\s+|•{2,4}|-)(\d{4})\b", str(val), re.I)
        if m:
            return m.group(1)
    return None


def _extract_merchant(tx: dict[str, Any]) -> str:
    # Activity Design Refresh: counterpartyInfo.counterpartyName
    cp_info = tx.get("counterpartyInfo")
    if isinstance(cp_info, dict):
        for k in ("counterpartyName", "name", "displayName", "businessName"):
            v = cp_info.get(k)
            if isinstance(v, str) and v.strip():
                return re.sub(r"\s+", " ", v).strip()

    for key in ("counterpartyBizName", "displayName", "counterparty", "merchantName", "name", "title", "payeeName"):
        val = tx.get(key)
        if isinstance(val, dict):
            val = val.get("name") or val.get("businessName") or val.get("displayName") or val.get("counterpartyName") or val.get("email")
        if isinstance(val, str) and val.strip():
            return re.sub(r"\s+", " ", val).strip()
    return ""


def _extract_description(tx: dict[str, Any]) -> str:
    parts: list[str] = []
    generic_desc = {
        "payment",
        "purchase",
        "purchase amount",
        "preapproved payment",
        "automatic payment",
        "express checkout payment",
        "mobile payment",
        "subtotal",
        "tax",
        "sales tax",
        "shipping",
        "shipping and handling",
        "total",
        "discount",
    }

    def _add(text: Any) -> None:
        if not isinstance(text, str):
            return
        s = re.sub(r"\s+", " ", text).strip().strip('"').strip()
        if s and s.lower() not in generic_desc and s not in parts:
            parts.append(s)

    def _add_item_list(items: Any) -> None:
        if not isinstance(items, list):
            return
        for it in items:
            if not isinstance(it, dict):
                continue
            t = it.get("name") or it.get("title") or it.get("itemName") or it.get("description")
            if not t:
                continue
            s = re.sub(r"\s+", " ", str(t)).strip().strip('"').strip()
            if not s or s.lower() in generic_desc:
                continue
            qty = it.get("quantity") or it.get("qty") or 1
            entry = f"{s} (x{qty})" if str(qty) not in ("1", "1.0", "", "None") else s
            if entry not in parts:
                parts.append(entry)

    # Inline detail item lists (top-level or nested under itemDetails)
    item_details = tx.get("itemDetails")
    if isinstance(item_details, dict):
        _add_item_list(item_details.get("primeItemList"))
        _add_item_list(item_details.get("itemList"))
    for list_key in ("items", "cartItems", "itemList"):
        _add_item_list(tx.get(list_key))

    # Activity Design Refresh: notesInfo.notes & descriptionInfo.content.transactionDescriptionText
    notes_info = tx.get("notesInfo")
    if isinstance(notes_info, dict):
        _add(notes_info.get("notes"))
    desc_info = tx.get("descriptionInfo")
    if isinstance(desc_info, dict):
        content = desc_info.get("content") or desc_info.get("contents")
        if isinstance(content, dict):
            _add(content.get("transactionDescriptionText"))
            _add(content.get("descriptionText"))

    for key in ("itemTitle", "itemName", "customNote", "note", "memo", "description", "type", "txnType"):
        _add(tx.get(key))
    return "; ".join(parts[:5])


def _extract_tx_id(raw: dict[str, Any]) -> str:
    for k in ("id", "transactionId", "encryptedTransactionId", "txnId"):
        v = raw.get(k)
        if v:
            return str(v).strip()

    link_candidates: list[str] = []
    links_info = raw.get("linksInfo")
    if isinstance(links_info, dict):
        for lk in ("detailsLink", "inlineDetailsLink", "link", "url", "href"):
            if isinstance(links_info.get(lk), str):
                link_candidates.append(links_info[lk])
    for lk in ("detailsLink", "link", "url", "href"):
        if isinstance(raw.get(lk), str):
            link_candidates.append(raw[lk])

    for lv in link_candidates:
        m = re.search(r"/details/(?:inline/)?([A-Za-z0-9_-]+)", lv)
        if m:
            return m.group(1)
        m2 = re.search(r"/transactions/([A-Za-z0-9_-]{8,})", lv)
        if m2 and m2.group(1) not in ("details", "filter", "inline"):
            return m2.group(1)
    return ""


def normalize_paypal_transaction(raw: dict[str, Any], default_year: int | None = None) -> dict[str, Any] | None:
    """Normalize a raw PayPal activity dict into our standard charge structure."""
    tx_id = _extract_tx_id(raw)
    if not tx_id:
        return None

    yr = raw.get("year") or default_year or dt.date.today().year
    fallback_year = int(yr) if str(yr).isdigit() else dt.date.today().year

    date_str = None
    for dk in (
        "timeCreated",
        "dateTime",
        "dateInfo",
        "transactionDateInfo",
        "date",
        "transactionDate",
        "formattedDate",
        "displayDate",
        "createTime",
        "time",
    ):
        if raw.get(dk) is not None:
            date_str = _parse_date(raw.get(dk), fallback_year=fallback_year)
            if date_str:
                break
    # Fallback: scan any key containing "date" or "time" (handles refreshed *Info names)
    if not date_str:
        for k, v in raw.items():
            if v is not None and ("date" in k.lower() or "time" in k.lower()):
                date_str = _parse_date(v, fallback_year=fallback_year)
                if date_str:
                    break
    if not date_str:
        return None

    is_credit = raw.get("isCredit")
    if not isinstance(is_credit, bool):
        if isinstance(raw.get("isDebit"), bool):
            is_credit = not raw["isDebit"]
        else:
            direction = str(raw.get("direction") or raw.get("creditDebit") or "").lower()
            amt_cls = ""
            if isinstance(raw.get("amountInfo"), dict):
                amt_cls = str((raw["amountInfo"].get("classNames") or {}).get("transactionAmountClassName") or "").lower()
            if direction in ("credit", "in", "cr") or "credit" in amt_cls or "positive" in amt_cls:
                is_credit = True
            elif direction in ("debit", "out", "dr") or "debit" in amt_cls or "negative" in amt_cls:
                is_credit = False
            else:
                is_credit = None

    amount = None
    for ak in (
        "amountInfo",
        "amounts",
        "grossAmount",
        "netAmount",
        "amount",
        "formattedAmount",
        "totalAmount",
        "displayAmount",
    ):
        amount = _parse_amount_float(raw.get(ak), is_credit=is_credit)
        if amount is not None:
            break
    if amount is None or abs(amount) < 0.005:
        return None

    # Default unsigned purchases to negative (outflow) unless clearly a credit/refund
    if is_credit is None and amount > 0:
        desc_text = ""
        if isinstance(raw.get("descriptionInfo"), dict):
            desc_text = str((raw["descriptionInfo"].get("content") or {}).get("transactionDescriptionText") or "")
        t_str = f"{raw.get('type') or ''} {raw.get('transactionType') or ''} {raw.get('status') or ''} {desc_text}".lower()
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
    default_year = None
    if isinstance(payload, dict):
        cur = payload.get("data")
        if isinstance(cur, dict) and isinstance(cur.get("data"), dict):
            cur = cur["data"]
        if isinstance(cur, dict) and str(cur.get("currentYear") or "").isdigit():
            default_year = int(cur["currentYear"])

    raw_list = _walk_for_transactions(payload)
    out = []
    for raw in raw_list:
        norm = normalize_paypal_transaction(raw, default_year=default_year)
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
