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
import html
import json
import os
import re
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

# XHR capture regex. Broad on purpose: matches every iosite call so
# we don't accidentally filter out the actual data XHR if RA tweaks
# the URL shape. _extract_complaints() does the structural filtering.
#
# Previous, tighter pattern was:
#   iosite\.reclameaqui\.com\.br/raichu-io-site-v1/companyshowcase/company/\d+/items
# but that missed any sibling endpoint like /complaints, /reviews, etc.
XHR_PATTERN = r"iosite\.reclameaqui\.com\.br"


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
        # Scrapling's captured XHRs may expose URL via `url` attribute
        # or via a nested request dict. Try both for safety.
        url = getattr(xhr, "url", "") or ""
        if not url and hasattr(xhr, "request"):
            req = getattr(xhr, "request", None)
            if isinstance(req, dict):
                url = req.get("url", "") or ""
        # Shorten the URL for log readability — keep the path tail.
        url_short = url.split("iosite.reclameaqui.com.br")[-1] if url else "?"
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
            print(f"[ra:{debug_slug}] xhr[{i}] {url_short} empty status={status}", flush=True)
            continue
        try:
            data = json.loads(body_str)
        except json.JSONDecodeError:
            print(f"[ra:{debug_slug}] xhr[{i}] {url_short} not-json status={status} preview={preview!r}", flush=True)
            continue
        if isinstance(data, list):
            print(f"[ra:{debug_slug}] xhr[{i}] {url_short} bare-list len={len(data)}", flush=True)
            out.extend(c for c in data if isinstance(c, dict))
            continue
        if not isinstance(data, dict):
            print(f"[ra:{debug_slug}] xhr[{i}] {url_short} non-dict {type(data).__name__}", flush=True)
            continue
        items = data.get("data") or data.get("items") or data.get("complaints") or data.get("results")
        if isinstance(items, list) and items:
            print(
                f"[ra:{debug_slug}] xhr[{i}] {url_short} OK keys={list(data.keys())} items={len(items)}",
                flush=True,
            )
            out.extend(c for c in items if isinstance(c, dict))
        else:
            print(
                f"[ra:{debug_slug}] xhr[{i}] {url_short} empty-list keys={list(data.keys())} preview={preview!r}",
                flush=True,
            )
    return out


def _parse_html_listing(body: str, slug: str) -> list[ScrapedPost]:
    """HTML-parsing fallback for when XHR capture yields nothing.

    RA's /lista-reclamacoes/ is SSR-rendered for SEO, so the first
    page of complaint cards is in the initial HTML. Each complaint
    is wrapped in markup that includes:
      - href="/empresa/{slug}/relato/{ID}/{title-slug}"   (the URL)
      - an adjacent <h4> or <h3> with the complaint title

    The XHR path was preferred because it gave us creationDate +
    description + status. The HTML path only yields title + URL,
    but that's enough for our keyword-search pipeline downstream.
    """
    # 1. Pull every relato href. Each href is the canonical URL for
    # one complaint and embeds the title in its trailing slug, which
    # we fall back to when the surrounding markup doesn't expose a
    # <h3>/<h4>.
    href_pattern = re.compile(
        rf'href="(/empresa/{re.escape(slug)}/relato/[^"]+)"'
    )
    href_matches = href_pattern.findall(body)

    # Deduplicate (RA frequently emits the same href in card + "open"
    # button markup) while preserving order.
    seen_urls: set[str] = set()
    ordered_hrefs: list[str] = []
    for h in href_matches:
        if h not in seen_urls:
            seen_urls.add(h)
            ordered_hrefs.append(h)

    # 2. For each href, look for the nearest preceding/following
    # <h3>/<h4> title. RA wraps card content in markup like:
    #   <a href="..."><h3>Title</h3></a>
    # or sometimes the title is in a sibling. Try the inline-link
    # pattern first, then fall back to the URL-slug derivation.
    inline_title_pattern = re.compile(
        rf'href="(/empresa/{re.escape(slug)}/relato/[^"]+)"[^>]*>'
        r'\s*<h[34][^>]*>([^<]{8,200})</h[34]>'
    )
    inline_titles: dict[str, str] = {}
    for url, title in inline_title_pattern.findall(body):
        inline_titles[url] = html.unescape(title).strip()

    out: list[ScrapedPost] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for href in ordered_hrefs:
        url = f"{RA_BASE}{href}"
        title = inline_titles.get(href)
        if not title:
            # Derive from URL slug: /relato/12345678/i-have-a-problem
            # → "I have a problem"
            tail = href.rstrip("/").rsplit("/", 1)[-1]
            title = tail.replace("-", " ").strip().capitalize()
        if not title:
            continue
        out.append(ScrapedPost(
            source="reclameaqui",
            title=title[:300],
            content=title,  # HTML path doesn't carry the body
            url=url,
            subreddit=slug,
            upvotes=0,
            comments_count=0,
            author=None,
            created_at=now_iso,
        ))
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
            # Give the SPA time to hydrate before scrolling — RA's
            # bundle is heavy and the previous 2s wait wasn't enough.
            page.wait_for_timeout(3000)
            # Scroll deeper to make sure we trip the data XHR. The
            # SPA waits until the complaint list enters viewport
            # before firing its first iosite call.
            for _ in range(6):
                page.mouse.wheel(0, 1800)
                page.wait_for_timeout(1500)
            page.wait_for_timeout(2500)
            return page

        try:
            resp = StealthyFetcher.fetch(
                url,
                headless=True,
                network_idle=True,
                timeout=90000,
                wait=3000,
                page_action=trigger_lazy_load,
                capture_xhr=XHR_PATTERN,
                # disable_resources was blocking JS chunks the SPA
                # needs to fire its data XHR — left enabled meant
                # every captured XHR came back empty. Letting CSS/
                # JS/fonts through costs ~1MB extra per page but is
                # the difference between empty and useful payload.
                disable_resources=False,
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
            # RA's SPA stopped firing param'd /items XHRs — the
            # /items endpoint now returns an all-null template for
            # bare calls. Fall back to parsing the SSR-rendered
            # HTML body, which still ships the first page of
            # complaint cards for SEO.
            body = getattr(resp, "body", None)
            if isinstance(body, bytes):
                body_str = body.decode("utf-8", errors="replace")
            else:
                body_str = str(body or "")
            html_posts = _parse_html_listing(body_str, slug)
            if html_posts:
                added = 0
                for post in html_posts:
                    key = post.url or post.title
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(post)
                    added += 1
                print(
                    f"[ra:{slug}] page {page_no} html-fallback: "
                    f"parsed {len(html_posts)} cards, {added} new",
                    flush=True,
                )
                # HTML fallback yields the FIRST page only — RA's
                # pagination is JS-driven and our XHR capture isn't
                # delivering it. Stop here; we'll get fresh page-1
                # data again on the next run.
                if added == 0:
                    break
                time.sleep(1.5)
                continue
            print(
                f"[ra:{slug}] page {page_no} captured {len(captured)} XHRs "
                f"+ html-fallback yielded 0 — stopping",
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
    # Slugs verified live in run 25675063154. The previously-listed
    # `cashu`, `banco-inter`, `magazine-luiza`, `americanas` all 404
    # — RA slug format is sometimes different from what marketing
    # implies (e.g., `magazineluiza-001` vs `magazine-luiza`). Stick
    # to the four we know work; we'll add more after probing.
    brands = args if args else [
        "nubank", "c6-bank", "picpay", "mercado-pago",
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
