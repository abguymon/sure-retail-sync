# Sure Retail Sync

Itemize, categorize, and split Amazon credit/debit card charges in your self-hosted [Sure Finance](https://github.com/we-promise/sure) instance — **without storing Amazon passwords, TOTP secrets, or fighting CAPTCHAs**.

## How It Works

Instead of running a headless scraper inside Docker (which constantly hits Amazon AWS WAF / CAPTCHAs and breaks with passkeys), **Sure Retail Sync** splits the work cleanly:

```
Chrome Extension (your real signed-in session)
  ├─ Fetches Amazon Transactions + only unseen Order Details HTML
  └─ POSTs HTML + bearer token ──► Docker Companion Server (Python)
                                      ├─ Parses HTML via amazon-orders (no JS selector drift)
                                      ├─ Matches Sure charges (card last-4 + exact cents + ±7d window)
                                      ├─ Categorizes each item (Jev / OpenAI / Ollama / Notes-only)
                                      └─ Writes itemized notes & auto-splits mixed-category charges
```

### Features
- **Passkey & 2FA friendly:** Uses your normal signed-in browser session — zero Amazon credentials stored on the server.
- **No Playwright / Chromium in Docker:** Lightweight `python:3.12-slim` image (~150 MB).
- **Smart matching & splitting:** Handles multi-shipment billing, partial gift-card/points discounts, refunds, and folds tiny sub-10% add-ons into the dominant category.
- **Safe by default:** Never overwrites transactions marked `user_modified`, already split, or previously synced (`state.json`). Supports **Dry Run** previews right in the popup.
- **Pluggable categorizer:** Supports TypeSafe **Jev**, any **OpenAI-compatible** endpoint (OpenAI, Ollama, OpenRouter, Groq), or **notes-only** mode.

---

## 1. Run the Sync Server

Copy `.env.example` to `.env`, fill in `SURE_URL`, `SURE_API_KEY`, a random `SYNC_API_TOKEN` (`openssl rand -hex 32`), and your preferred categorizer, then start the container:

```bash
cp .env.example .env
docker compose -f docker-compose.example.yml up -d --build
```

Expose port `8080` to your browser via your reverse proxy (Traefik/Caddy/Nginx) or Tailscale. All `/api/*` endpoints require `Authorization: Bearer <SYNC_API_TOKEN>`.

## 2. Install the Chrome Extension

1. Open `chrome://extensions` in Chrome, Brave, or Edge.
2. Enable **Developer mode** (top right).
3. Click **Load unpacked** and select the `extension/` folder from this repo.
4. Click the **Sure Retail Sync** toolbar icon → **Settings**, enter your server URL (e.g. `https://sure.example.com/sync`) and `SYNC_API_TOKEN`, and click **Save & Test Connection**.
5. Click **Dry Run** to preview matches, then **Sync Amazon**!

## API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | None | Healthcheck (`{"ok": true}`) |
| `GET` | `/api/status` | Bearer | Checks Sure API connectivity, active categorizer, and cache counts |
| `POST` | `/api/amazon/plan` | Bearer | Parses Amazon Transactions HTML/JSON, matches Sure, returns unseen order URLs + pagination |
| `POST` | `/api/amazon/sync` | Bearer | Parses Order Details HTML, categorizes items, updates notes & splits in Sure |

## Running Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
