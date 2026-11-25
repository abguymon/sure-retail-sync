"""Pluggable item categorizers for Sure Retail Sync.

Supported providers (set via CATEGORIZER env var):
- "jev"    : TypeSafe Jev (requires JEV_API_KEY)
- "openai" : OpenAI or any OpenAI-compatible API including Ollama/OpenRouter/Groq
             (uses OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL)
- "none"   : Notes-only mode (writes itemized notes without changing categories)
"""

import json
import logging
import os
import requests

log = logging.getLogger("sure-retail-sync.categorizer")

UNSURE = "__uncategorized__"


class Categorizer:
    name = "none"

    def categorize(self, store: str, items: list[dict], categories: dict[str, str]) -> list[str | None]:
        raise NotImplementedError


class NullCategorizer(Categorizer):
    name = "none"

    def categorize(self, store: str, items: list[dict], categories: dict[str, str]) -> list[str | None]:
        return [None for _ in items]


class JevCategorizer(Categorizer):
    name = "jev"

    def __init__(
        self,
        api_key: str,
        endpoint: str = "https://api.typesafe.ai/v1/systemone",
        model: str = "jev-latest",
        fallback_category: str = "Shopping",
        min_confidence: float = 0.5,
        category_hints: dict[str, str] | None = None,
    ):
        self.api_key = api_key
        self.endpoint = endpoint
        self.model = model
        self.fallback_category = fallback_category
        self.min_confidence = min_confidence
        self.category_hints = category_hints or {}

    def categorize(self, store: str, items: list[dict], categories: dict[str, str]) -> list[str | None]:
        criteria = {name: self.category_hints.get(name) for name in categories}
        criteria[UNSURE] = "No listed category is a reasonable fit"
        questions = {
            f"item_{i}": {
                "type": "choice",
                "instructions": (
                    f"Which household spending category best fits this {store} purchase: {it['title']!r}? "
                    f"Pick `{UNSURE}` only if no category is a reasonable fit."
                ),
                "criteria": criteria,
            }
            for i, it in enumerate(items)
        }
        state = {
            "store": store,
            "items": [{"title": it["title"], "price": it["price"]} for it in items],
        }
        r = requests.post(
            self.endpoint,
            timeout=60,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "state": state, "questions": questions},
        )
        r.raise_for_status()
        answers = r.json().get("answers", {})
        fallback = self.fallback_category if self.fallback_category in categories else None
        out = []
        for i in range(len(items)):
            a = answers.get(f"item_{i}", {})
            choice = a.get("choice")
            conf = a.get("confidence") or 0
            if choice in categories and conf >= self.min_confidence:
                out.append(choice)
            else:
                out.append(fallback)
        return out


class OpenAICompatibleCategorizer(Categorizer):
    name = "openai"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        fallback_category: str = "Shopping",
        category_hints: dict[str, str] | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.fallback_category = fallback_category
        self.category_hints = category_hints or {}

    def categorize(self, store: str, items: list[dict], categories: dict[str, str]) -> list[str | None]:
        cat_desc = [
            f"{name}: {self.category_hints[name]}" if name in self.category_hints else name
            for name in sorted(categories.keys())
        ]
        prompt = (
            f"Classify each {store} purchase item into exactly one category from this list:\n"
            f"{json.dumps(cat_desc)}\n\n"
            f"Items:\n"
            f"{json.dumps([{'index': i, 'title': it['title'], 'price': it['price']} for i, it in enumerate(items)])}\n\n"
            'Return ONLY valid JSON: {"categories": ["<exact category name for index 0>", ...]} '
            "with the exact same length as the items array."
        )
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        r = requests.post(
            f"{self.base_url}/chat/completions",
            timeout=60,
            headers=headers,
            json={
                "model": self.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": "You categorize household purchases strictly using provided category names."},
                    {"role": "user", "content": prompt},
                ],
            },
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        chosen = parsed.get("categories", [])
        fallback = self.fallback_category if self.fallback_category in categories else None
        out = []
        for i in range(len(items)):
            c = chosen[i] if i < len(chosen) else None
            out.append(c if c in categories else fallback)
        return out


def _parse_category_hints() -> dict[str, str]:
    raw = os.environ.get("CATEGORY_HINTS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items() if str(v).strip()}
    except json.JSONDecodeError as exc:
        log.warning("Invalid CATEGORY_HINTS JSON (ignoring): %s", exc)
    return {}


def build_categorizer() -> Categorizer:
    provider = os.environ.get("CATEGORIZER", "").strip().lower()
    fallback = os.environ.get("FALLBACK_CATEGORY", "Shopping")
    min_conf = float(os.environ.get("MIN_CONFIDENCE", "0.5"))
    hints = _parse_category_hints()

    # Auto-detect if CATEGORIZER is omitted
    if not provider:
        if os.environ.get("JEV_API_KEY"):
            provider = "jev"
        elif os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_BASE_URL"):
            provider = "openai"
        else:
            provider = "none"

    if provider == "jev":
        key = os.environ.get("JEV_API_KEY", "").strip()
        if not key:
            log.warning("CATEGORIZER=jev but JEV_API_KEY is empty; falling back to notes-only mode")
            return NullCategorizer()
        return JevCategorizer(
            api_key=key,
            endpoint=os.environ.get("JEV_ENDPOINT", "https://api.typesafe.ai/v1/systemone"),
            model=os.environ.get("JEV_MODEL", "jev-latest"),
            fallback_category=fallback,
            min_confidence=min_conf,
            category_hints=hints,
        )

    if provider == "openai":
        return OpenAICompatibleCategorizer(
            api_key=os.environ.get("OPENAI_API_KEY", "").strip(),
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            fallback_category=fallback,
            category_hints=hints,
        )

    return NullCategorizer()
