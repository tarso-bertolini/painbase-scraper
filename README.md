# painbase-scraper

The Tier-3 scraping engine for [Painbase](https://github.com/tarso-bertolini/painbase) — Playwright workloads that run on GitHub Actions and POST results back to the Painbase ingest API.

## Why a separate repo

The main Painbase product runs on Vercel, where:
- Functions time out at 60 seconds (Pro plan)
- There's no headless browser
- Crons can't realistically launch Playwright

Scraping targets that gate behind Cloudflare or other JS challenges (Reclame Aqui, Mercado Livre, JusBrasil) need a real browser. GitHub Actions gives us that, **for free, with unlimited minutes on a public repo**. So we use this repo as a scraping fleet and push the structured output back to Painbase.

This repo is intentionally public — that's what unlocks unlimited GHA minutes. There are no secrets in source; the only secret is `INGEST_TOKEN`, set as a GitHub Actions repo secret.

## What lives here

```
.github/workflows/    Cron-scheduled and manual-dispatch scraping workflows
scripts/scrape/       Playwright scrapers, one per target source
lib/ingest-client.ts  POSTs scraped Posts back to the Painbase ingest API
lib/types.ts          Shared types (mirrors the Painbase Post shape)
```

## How a scrape runs

1. GHA cron fires (e.g. every 6h for `scrape-reclameaqui`).
2. Workflow checks out, installs deps, launches Playwright.
3. The script visits each target page, parses what's on the DOM, and accumulates a `Post[]` matching the Painbase schema.
4. Results are POST'd in batches to `https://painbase.com.br/api/ingest/raw-scrapes` with `Authorization: Bearer $INGEST_TOKEN`.
5. The Painbase API upserts each row using the existing `ON CONFLICT (source, url)` dedup logic.

## Local development

```bash
pnpm install
pnpm playwright install chromium
INGEST_URL=http://localhost:3000/api/ingest/raw-scrapes \
INGEST_TOKEN=... \
pnpm tsx scripts/scrape/reclameaqui.ts nubank
```

Pass `--dry-run` to skip the POST and dump JSON to stdout — useful for verifying selectors before wiring up the API.

## Adding a new target

1. Drop a script at `scripts/scrape/<source>.ts` exporting a `default async function (brand: string): Promise<Post[]>`.
2. Add a workflow at `.github/workflows/scrape-<source>.yml` that imports the same shared runner glue.
3. Test locally with `--dry-run`. Verify selectors. Verify ingest contract.

## Ethics + safety

- Run-time is throttled (≥1s between requests, ≥2s between page navigations) to never burden the target.
- We respect `robots.txt` for everything we don't have a separate legal basis to bypass.
- Authentication / paywall content is off-limits.
- No real-user credentials are stored in this repo. Throwaway accounts only, all stored as GHA secrets.
