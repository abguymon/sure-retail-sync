"""Sure Finance REST API v1 client."""

import re
import requests

DEFAULT_NOT_SPENDING = {"Income", "Investment Contributions", "Savings & Investments"}
AMAZON_TX_RE = re.compile(r"amazon|amzn|amz\*|prime video|kindle|audible", re.I)


class SureClient:
    def __init__(self, base_url: str, api_key: str, dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.headers["X-Api-Key"] = api_key

    def _get(self, path: str, **params):
        r = self.session.get(f"{self.base_url}/api/v1/{path}", params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def paged(self, path: str, key: str, **params):
        page = 1
        while True:
            body = self._get(path, page=page, per_page=100, **params)
            yield from body[key]
            if page >= body["pagination"]["total_pages"]:
                return
            page += 1

    def spending_categories(self, exclude_names=None) -> dict[str, str]:
        """Return leaf spending categories mapped {name: id}."""
        excluded = set(exclude_names or DEFAULT_NOT_SPENDING)
        cats = list(self.paged("categories", "categories"))
        income_ids = {c["id"] for c in cats if c["name"] == "Income"}
        return {
            c["name"]: c["id"]
            for c in cats
            if c["name"] not in excluded
            and c.get("subcategories_count", 0) == 0
            and (c.get("parent") or {}).get("id") not in income_ids
        }

    def amazon_transactions(self, start_date, end_date, include_depository: bool = True) -> list[dict]:
        """Return Sure transactions in [start_date, end_date] that look like Amazon charges."""
        allowed_types = {"credit_card", "depository"} if include_depository else {"credit_card"}
        out = []
        for t in self.paged(
            "transactions",
            "transactions",
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        ):
            acc_type = (t.get("account") or {}).get("account_type", "")
            if acc_type in allowed_types and AMAZON_TX_RE.search(t.get("name") or ""):
                out.append(t)
        return out

    def update_transaction(self, txn_id: str, **fields) -> None:
        if self.dry_run:
            return
        r = self.session.patch(
            f"{self.base_url}/api/v1/transactions/{txn_id}",
            json={"transaction": fields},
            timeout=60,
        )
        r.raise_for_status()

    def split_transaction(self, txn_id: str, splits: list[dict]) -> None:
        if self.dry_run:
            return
        r = self.session.post(
            f"{self.base_url}/api/v1/transactions/{txn_id}/split",
            json={"split": {"splits": splits}},
            timeout=60,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"split failed {r.status_code}: {r.text[:300]}")
