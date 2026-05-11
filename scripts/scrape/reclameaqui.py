"""Reclame Aqui scraper — Scrapling/StealthyFetcher with XHR capture.

The story so far:
    Initial attempt: hit iosite.reclameaqui.com.br directly. CF 403'd
    every request including from a browser session that had already
    cleared the challenge.

    Fix: don't fetch iosite ourselves. Visit the listing page in a
    real Camoufox browser, let the SPA make ITS OWN XHR calls to
    iosite (which carry whatever auth header the SPA generates), and
    intercept the JSON responses via Scrapling's `capture_xhr` regex.

Endpoint discovered via the probe at scripts/probe_ra_xhr.py:
    https://iosite.reclameaqui.com.br/raichu-io-site-v1/companyshowcase/
    company/{numeric_id}/items

    Returns JSON: { prev, next, data: [...], count, maxScore, aggregations }
    where each `data[]` row is a complaint with title, description,
    creationDate, status, url, etc.

Pagination: re-navigate to the listing page with ?pagina=2, ?pagina=3
each triggers a fresh XHR round we capture.

Run locally:
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

# Number of paginated URLs to walk per brand. Each navigation = one
# capture round. RA serves ~10 complaints per page on the listing.
MAX_PAGES_PER_BRAND = 5

# XHR capture regex. Matches the JSON endpoint the SPA actually calls.
XHR_PATTERN = (
    r"iosite\.reclameaqui\.com\.br/raichu-io-site-v1/"
    r"companyshowcase/company/\d+/items"
)


def _build_complaint_url(c: dict[str, Any], slug: str) -> str | None:
    raw_url = c.get("url")
    if isinstance(raw_url, str) and raw_url:
        return raw_url if raw_url.startswith("http") else f"{RA_BASE}{raw_url}"
    cid = c.get("id")
    if cid:
        return f"{RA_BASE}/empresa/{slug}/relato/{cid}/"
    return None


def _normalize(c: dict[str, Any], slug: str) -> ScrapedPost | None:
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
        # Reuse the subreddit slot for the company slug — matches the
        # convention used elsewhere so the watch dashboard's
        # "Companies" rollup works across sources.
        subreddit=slug,
        upvotes=0,
        comments_count=0,
        author=str(c.get("userFirstName") or c.get("userName") or "") or None,
        created_at=created_iso,
    )


def _extract_complaints(captured_xhr_list: list, debug_slug: str = "") -> list[dict[str, Any]]:
    """Pull complaint dicts out of every captured XHR response.

    Each `companyshowcase/company/{id}/items` response carries a
    `data` array. Concat across all captures, return raw dicts.

    When the upstream shape shifts (RA changes field names, payload
    becomes an array instead of an object, etc.) we log a 200-char
    preview of each XHR body so the next run's CI log tells us what
    to adjust without another local probe trip.
    """
    out: list[dict[str, Any]] = []
    for i, xhr in enumerate(captured_xhr_list):
        url = getattr(xhr, "url", "") or getattr(xhr, "request", {}).get("url", "") if hasattr(xhr, "request") else ""
        status = getattr(xhr, "status", "?")
        body = getattr(xhr, "body", None)
        if body is None:
            text = getattr(xhr, "text", "")
            body = text.encode("utf-8") if text else b""
        if isinstance(body, bytes):
            body_str = body.decode("utf-8", errors="replace")
        else:
            body_str = str(body)
        preview = body_str[:200].replace("\n", " ") if body_str else "(empty)"
        if not body_str:
            print(f"[ra:{debug_slug}] xhr[{i}] empty body url={url} status={status}", flush=True)
            continue
        try:
            data = json.loads(body_str)
        except json.JSONDecodeError:
            print(f"[ra:{debug_slug}] xhr[{i}] not-json status={status} preview={preview!r}", flush=True)
            continue
        if isinstance(data, list):
            # RA might have flattened the response to a bare array.
            print(f"[ra:{debug_slug}] xhr[{i}] bare-list len={len(data)} preview={preview!r}", flush=True)
            out.extend(c for c in data if isinstance(c, dict))
            continue
        if not isinstance(data, dict):
            print(f"[ra:{debug_slug}] xhr[{i}] non-dict {type(data).__name__} preview={preview!r}", flush=True)
            continue
        items = data.get("data") or data.get("items") or data.get("complaints") or data.get("results")
        if isinstance(items, list):
            print(
                f"[ra:{debug_slug}] xhr[{i}] ok keys={list(data.keys())} items={len(items)}",
                flush=True,
            )
            out.extend(c for c in items if isinstance(c, dict))
        else:
            print(
                f"[ra:{debug_slug}] xhr[{i}] dict no-list-field keys={list(data.keys())} preview={preview!r}",
                flush=True,
            )
    return out


def scrape_brand(slug: str) -> list[ScrapedPost]:
    """Pull complaints for one company slug across MAX_PAGES_PER_BRAND
    paginated visits. Returns ScrapedPost rows; never raises."""
    print(f"[ra:{slug}] launching StealthyFetcher (XHR capture)...", flush=True)

    out: list[ScrapedPost] = []
    seen: set[str] = set()

    for page_no in range(1, MAX_PAGES_PER_BRAND + 1):
        # /lista-reclamacoes/ is the SPA-rendered listing page. Each
        # navigation triggers a fresh round of iosite XHRs we capture.
        url = (
            f"{RA_BASE}/empresa/{slug}/lista-reclamacoes/"
            if page_no == 1
            else f"{RA_BASE}/empresa/{slug}/lista-reclamacoes/?pagina={page_no}"
        )

        def trigger_lazy_load(page):
            # Scroll a few viewports to make the SPA load its lazy
            # complaint cards (which is what triggers the XHR).
            for _ in range(4):
                page.mouse.wheel(0, 1500)
                page.wait_for_timeout(1000)
            page.wait_for_timeout(1500)
            return page

        try:
            resp = StealthyFetcher.fetch(
                url,
                headless=True,
                network_idle=True,
                timeout=90000,
                wait=2000,
                page_action=trigger_lazy_load,
                capture_xhr=XHR_PATTERN,
                disable_resources=True,
                solve_cloudflare=True,
                humanize=True,
            )
        except Exception as e:
            print(f"[ra:{slug}] page {page_no} fetch failed: {e}", flush=True)
            break

        if resp is None or getattr(resp, "status", None) not in (200, 304):
            print(
                f"[ra:{slug}] page {page_no} status="
                f"{getattr(resp, 'status', '?')} — stopping",
                flush=True,
            )
            break

        captured = list(getattr(resp, "captured_xhr", []) or [])
        complaints = _extract_complaints(captured, debug_slug=slug)
        if not complaints:
            print(
                f"[ra:{slug}] page {page_no} captured {len(captured)} XHRs "
                f"but 0 complaints in payload — stopping",
                flush=True,
            )
            break

        added = 0
        for c in complaints:
            post = _normalize(c, slug)
            if post is None:
                continue
            key = post.url or post.title
            if key in seen:
                continue
            seen.add(key)
            out.append(post)
            added += 1

        print(
            f"[ra:{slug}] page {page_no}: {len(captured)} XHRs, "
            f"{len(complaints)} complaints, {added} new",
            flush=True,
        )

        # If this page yielded nothing new, the SPA returned the same
        # cursor — no point continuing.
        if added == 0:
            break

        time.sleep(1.5)  # polite throttle between page navigations

    print(f"[ra:{slug}] done — {len(out)} complaints", flush=True)
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    # `cashu` and `banco-inter` returned 404 on RA — slugs apparently
    # retired or renamed. Drop until we re-probe with current ones.
    # The remaining four are confirmed live as of the last manual run.
    brands = args if args else [
        "nubank", "c6-bank", "picpay", "mercado-pago",
        "magazine-luiza", "americanas",  # BR retail giants — high complaint volume
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
    print(
        f"[ra] ingest result: inserted={result.inserted} "
        f"duplicates={result.duplicates} skipped={result.skipped}",
        flush=True,
    )
    if result.errors:
        for err in result.errors:
            print(f"  ! {err}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
