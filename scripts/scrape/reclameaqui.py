"""Reclame Aqui scraper — Scrapling/StealthyFetcher edition.

Why Scrapling:
    The earlier Playwright + stealth-plugin TS implementation bounced
    off Cloudflare's Turnstile every time we probed RA's iosite JSON
    endpoint. Scrapling's StealthyFetcher is built on Camoufox — a
    Firefox fork specifically tuned for stealth — and ships purpose-
    built bypass logic for Cloudflare Turnstile/Interstitial.

    Documented bypass quote from Scrapling's README:
        "Can easily bypass all types of Cloudflare's Turnstile/
         Interstitial with automation."

    BSD-3-Clause license; we're free to use, modify, and ship.

Strategy (same shape as the older TS scraper):
    1. Hit the company's public HTML page first via StealthyFetcher.
       This solves the CF challenge on the page session.
    2. Then call the JSON endpoint at iosite.reclameaqui.com.br
       through the same fetcher session — the CF cookie carries.
    3. Walk the paginated complaint list per brand, normalize to the
       Painbase ScrapedPost shape, and POST to the ingest API.

Run locally (after `pip install -r requirements.txt`):
    python scripts/scrape/reclameaqui.py nubank --dry-run
    python scripts/scrape/reclameaqui.py nubank cashu c6-bank

Run on GHA: see .github/workflows/scrape-reclameaqui.yml.
"""
from __future__ import annotations
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure the package is importable when running as a script (without
# `pip install -e .`). Adds the repo root to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scrapling.fetchers import StealthyFetcher  # type: ignore[import-not-found]

from painbase_scraper import ScrapedPost, post_scrapes_to_ingest

RA_BASE = "https://www.reclameaqui.com.br"
IOSITE = "https://iosite.reclameaqui.com.br/raichu-io-site-v1"
PAGE_SIZE = 30
MAX_PAGES_PER_BRAND = 3  # ~90 complaints/brand/run


def _build_complaint_url(c: dict[str, Any], slug: str) -> str | None:
    """Mirror the URL-builder from the TS scraper's normalize step."""
    raw_url = c.get("url")
    if isinstance(raw_url, str) and raw_url:
        return raw_url if raw_url.startswith("http") else f"{RA_BASE}{raw_url}"
    cid = c.get("id")
    if cid:
        return f"{RA_BASE}/empresa/{slug}/relato/{cid}/"
    return None


def _normalize(c: dict[str, Any], slug: str) -> ScrapedPost | None:
    """Translate a single RA JSON complaint into a Painbase ScrapedPost.

    Returns None when the row is malformed enough to skip (no title,
    no addressable URL).
    """
    title = str(c.get("title") or "").strip()
    if not title:
        return None
    url = _build_complaint_url(c, slug)
    if not url:
        return None

    created_raw = (
        c.get("creationDate") or c.get("created") or c.get("lastInteractionTime")
    )
    if created_raw:
        try:
            # RA serves ISO-ish dates; fall back to now() on parse failure.
            created_iso = (
                datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
                .astimezone(timezone.utc)
                .isoformat()
            )
        except (ValueError, TypeError):
            created_iso = datetime.now(timezone.utc).isoformat()
    else:
        created_iso = datetime.now(timezone.utc).isoformat()

    description = str(c.get("description") or "").strip() or title

    return ScrapedPost(
        source="reclameaqui",
        title=title[:300],
        content=description,
        url=url,
        # Reuse the subreddit slot for the company slug — matches the TS
        # convention so the watch dashboard's "Companies" rollup works
        # across both code paths.
        subreddit=slug,
        upvotes=0,
        comments_count=0,
        author=str(c.get("userFirstName") or c.get("userName") or "") or None,
        created_at=created_iso,
    )


def _extract_complaints(data: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    """Pull the complaint list out of any of the response shapes RA
    has used historically. Fail closed (empty list) on unknown shape.
    """
    if isinstance(data, list):
        return [c for c in data if isinstance(c, dict)]
    if not isinstance(data, dict):
        return []

    complains = data.get("complains")
    if isinstance(complains, list):
        return [c for c in complains if isinstance(c, dict)]
    if isinstance(complains, dict):
        inner = complains.get("data")
        if isinstance(inner, list):
            return [c for c in inner if isinstance(c, dict)]

    inner_data = data.get("data")
    if isinstance(inner_data, list):
        return [c for c in inner_data if isinstance(c, dict)]

    return []


def scrape_brand(slug: str) -> list[ScrapedPost]:
    """Pull up to MAX_PAGES_PER_BRAND × PAGE_SIZE complaints for one
    company slug and return them as ScrapedPost rows.

    Empty list on bypass failure — never raises, so a single broken
    brand can't kill the whole run.
    """
    print(f"[ra:{slug}] launching StealthyFetcher...", flush=True)

    out: list[ScrapedPost] = []
    seen: set[str] = set()

    # Step 1 — visit the company HTML page to clear the CF challenge.
    # `solve_cloudflare=True` tells Scrapling to wait through (and
    # solve, if Turnstile-style) the interstitial automatically.
    company_url = f"{RA_BASE}/empresa/{slug}/"
    fetcher = StealthyFetcher

    try:
        prime = fetcher.fetch(
            company_url,
            headless=True,
            solve_cloudflare=True,
            disable_resources=True,  # ad / image blocking — faster + less detection
            network_idle=True,
            humanize=True,           # add human-like cursor / typing jitter
            timeout=45000,
        )
    except Exception as e:
        print(f"[ra:{slug}] prime navigation failed: {e}", flush=True)
        return out

    if prime is None or getattr(prime, "status", None) not in (200, 304):
        print(f"[ra:{slug}] prime returned status="
              f"{getattr(prime, 'status', '?')} — bailing", flush=True)
        return out

    # Step 2 — call the JSON endpoint. StealthyFetcher.fetch reuses
    # the underlying browser session by default, so the CF cookie
    # carries automatically. Walk pages until we get an empty page or
    # a short page (last page).
    for page_no in range(MAX_PAGES_PER_BRAND):
        api_url = (
            f"{IOSITE}/companies/shortname/{slug}/complains"
            f"?count={PAGE_SIZE}&start={page_no * PAGE_SIZE}&filter=created_desc"
        )
        try:
            resp = fetcher.fetch(
                api_url,
                headless=True,
                solve_cloudflare=False,  # already solved on prime
                disable_resources=True,
                network_idle=False,
                timeout=30000,
            )
        except Exception as e:
            print(f"[ra:{slug}] page {page_no} fetch error: {e}", flush=True)
            break

        if resp is None or getattr(resp, "status", None) != 200:
            print(f"[ra:{slug}] page {page_no} status="
                  f"{getattr(resp, 'status', '?')} — stopping", flush=True)
            break

        # Scrapling responses expose .text (raw body). The endpoint
        # returns JSON; if it ever serves HTML, we treat as no-data.
        body = getattr(resp, "text", "") or ""
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            print(f"[ra:{slug}] page {page_no} non-JSON body — stopping", flush=True)
            break

        items = _extract_complaints(data)
        if not items:
            break

        added = 0
        for c in items:
            post = _normalize(c, slug)
            if post is None:
                continue
            key = post.url or post.title
            if key in seen:
                continue
            seen.add(key)
            out.append(post)
            added += 1

        print(f"[ra:{slug}] page {page_no}: {len(items)} fetched, "
              f"{added} new", flush=True)

        if len(items) < PAGE_SIZE:
            break

        time.sleep(1.0)  # polite throttle between pages

    print(f"[ra:{slug}] done — {len(out)} complaints", flush=True)
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    brands = args if args else [
        "nubank", "cashu", "c6-bank", "banco-inter", "picpay", "mercado-pago",
    ]

    run_id = os.environ.get("GITHUB_RUN_ID") or datetime.now(timezone.utc).isoformat()
    print(f"[ra] run {run_id} — brands: {', '.join(brands)}", flush=True)

    all_posts: list[ScrapedPost] = []
    for slug in brands:
        try:
            all_posts.extend(scrape_brand(slug))
        except Exception as e:
            print(f"[ra:{slug}] failed: {e}", file=sys.stderr, flush=True)

    print(f"[ra] total scraped: {len(all_posts)}", flush=True)

    if not all_posts:
        print("[ra] nothing to ingest", flush=True)
        return 0

    result = post_scrapes_to_ingest(
        all_posts, source="reclameaqui", run_id=run_id,
    )
    print(f"[ra] ingest result: inserted={result.inserted} "
          f"duplicates={result.duplicates} skipped={result.skipped}", flush=True)
    if result.errors:
        for err in result.errors:
            print(f"  ! {err}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
