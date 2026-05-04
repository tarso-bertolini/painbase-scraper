# painbase-scraper

Anti-bot scraping fleet for [Painbase](https://github.com/tarso-bertolini/painbase). Runs as GitHub Actions workflows that bypass JS challenges (Cloudflare Turnstile, etc.) and POST results back to the Painbase ingest API.

## Why a separate repo

The main Painbase product runs on Vercel, where:
- Functions time out at 60 seconds (Pro plan)
- There's no headless browser
- Crons can't realistically launch Playwright or Camoufox

Scraping targets that gate behind Cloudflare or other JS challenges (Reclame Aqui, Mercado Livre, JusBrasil) need a real, stealth-tuned browser. GitHub Actions gives us that, **for free, with unlimited minutes on a public repo**.

This repo is intentionally public — that's what unlocks unlimited GHA minutes. There are no secrets in source; the only secret is `INGEST_TOKEN`, set as a GitHub Actions repo secret.

## Architecture: bilingual by design

The repo runs **two scraping toolchains** because Cloudflare-gated and ungated targets need different tools:

| Toolchain | When to use | Tools |
|---|---|---|
| **Python (Scrapling)** | Cloudflare-gated targets that defeat plain Playwright | [Scrapling](https://github.com/D4Vinci/Scrapling) `StealthyFetcher` (built on Camoufox, a stealth-tuned Firefox fork). BSD-3-Clause. Solves CF Turnstile/Interstitial automatically. |
| **TypeScript (Playwright)** | Lightweight HTML parses on ungated public APIs / pages | playwright-extra + stealth plugin. Cheaper to run, easier for adapters that don't need the heavy stealth surface. |

Both toolchains publish to the same `painbase_scraper` / `lib/ingest-client` interface so adding a new source picks whichever toolchain fits the target's defenses.

## Repo layout

```
.github/workflows/         Cron + manual-dispatch scraping workflows (one per target)
scripts/scrape/            Scrapers, one file per target source (.py or .ts)

# Python toolchain
painbase_scraper/          Importable package
  __init__.py              Re-exports the public API
  types.py                 ScrapedPost dataclass (mirrors Painbase Post)
  ingest.py                post_scrapes_to_ingest() — batched POST to ingest API
requirements.txt           Python deps (Scrapling, httpx)

# TypeScript toolchain
lib/ingest-client.ts       Same contract as Python, for non-gated TS scrapers
lib/types.ts               ScrapedPost mirror
package.json
tsconfig.json
```

## How a scrape runs

1. GHA cron fires (e.g. every 6h for `scrape-reclameaqui`).
2. Workflow installs deps, fetches Camoufox / browser binaries (cached).
3. Script visits each target's page, accumulates a list of `ScrapedPost` rows.
4. Results POST in 50-row batches to `https://painbase.com.br/api/ingest/raw-scrapes` with `Authorization: Bearer $INGEST_TOKEN`.
5. The Painbase API upserts each row using `ON CONFLICT (source, url) DO UPDATE` — same dedup semantics as the in-app watch pipeline.

## Local development

### Python (Scrapling) target

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m camoufox fetch    # one-time browser download

# Dry run (no POST, just dumps JSON to stdout):
python scripts/scrape/reclameaqui.py nubank --dry-run

# Real run:
INGEST_URL=http://localhost:3000/api/ingest/raw-scrapes \
INGEST_TOKEN=... \
python scripts/scrape/reclameaqui.py nubank cashu c6-bank
```

### TypeScript target

```bash
pnpm install
pnpm playwright install chromium

INGEST_URL=http://localhost:3000/api/ingest/raw-scrapes \
INGEST_TOKEN=... \
pnpm tsx scripts/scrape/<target>.ts
```

`--dry-run` works on both — skips the POST, dumps JSON. Use it to verify selectors and the bypass is working before wiring the ingest path.

## Adding a new target

1. **Probe first**: hit the target with `curl` or browser DevTools. Identify whether it's Cloudflare/JS-gated. If yes → Python. If no → TypeScript is faster to maintain.
2. **Drop a script** at `scripts/scrape/<source>.{py,ts}`.
3. **Add a workflow** at `.github/workflows/scrape-<source>.yml`. Copy `scrape-reclameaqui.yml` for the Python pattern.
4. **Test locally with `--dry-run`** until selectors are stable.
5. **Add the source** to the allowlist in the main repo's `app/api/ingest/raw-scrapes/route.ts → VALID_SOURCES`.

## Ethics + safety

- Per-page throttling (≥1s between requests, ≥2s between navigations) — never DoS the target.
- We respect `robots.txt` unless we have a separate legal basis to bypass.
- Authentication / paywall content is off-limits.
- No real-user credentials in this repo. Throwaway accounts only, GHA secrets only.
- Rate-limited per source so a runaway loop can't burn through residential bandwidth or trip rate limits.

## Why Scrapling specifically

Scrapling is BSD-3-Clause (commercial-friendly), 44k stars, actively maintained. Its `StealthyFetcher` is the only library we tested that defeats Cloudflare Turnstile reliably from headless GHA runners.

What we get for free vs. building in-house:
- Camoufox-based stealth (TLS fingerprint + behavioral mimicry)
- Adaptive selectors (auto-relocate when a target's HTML changes)
- HTTP/3 + DNS-over-HTTPS to defeat IP-correlation
- Built-in proxy rotation
- Continuous updates as anti-bot vendors evolve

Trying to clone this in TypeScript would burn months for a worse result. The bilingual repo is the right tradeoff.
