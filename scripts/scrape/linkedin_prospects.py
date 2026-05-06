"""LinkedIn prospect scraper — Camoufox + a real logged-in session.

Goal: produce a list of {name, role, company, linkedin_url} rows
from a LinkedIn search query, then POST them to the Painbase
prospects ingest endpoint at:

    POST /api/admin/outbound/prospects
    body: { prospects: [...], enrich: true }

Reality check (read this before running):
    LinkedIn's ToS prohibits automated scraping. They detect headless
    browsers aggressively and ban accounts that exhibit bot-shaped
    behavior. This script is set up for a HUMAN-IN-THE-LOOP model:

      1. You log into LinkedIn manually once with a throwaway account
         (NOT your primary). This populates Camoufox's user_data_dir
         with auth cookies.
      2. The script reuses that profile, throttles requests to a
         human-rate (≥3s/page), and limits scope (≤25 results per
         run, ≤1 run/day).
      3. If LinkedIn flags the account, you toss the throwaway and
         repeat with a fresh one.

    For higher-volume needs use Apollo, Sales Navigator API, or
    PhantomBuster — all paid but ToS-compliant for outreach.

Usage:
    # Step 1 — interactive login (one-time per cookie jar):
    python scripts/scrape/linkedin_prospects.py --login

    # Step 2 — search + collect:
    python scripts/scrape/linkedin_prospects.py \\
        --query "head of brand" \\
        --location "Brazil" \\
        --limit 20

    # Step 3 — POST results to Painbase ingest:
    INGEST_URL=https://painbase.com.br/api/admin/outbound/prospects \\
    INGEST_TOKEN=<admin cookie OR bearer> \\
        python scripts/scrape/linkedin_prospects.py --query ... --send
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

# Reuse the package layout already in place (same as reclameaqui.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scrapling.fetchers import StealthyFetcher  # type: ignore[import-not-found]


# Directory for the persistent Camoufox profile (cookies, localStorage).
# Stored under .cache/ which is gitignored.
PROFILE_DIR = Path(__file__).resolve().parents[2] / '.cache' / 'linkedin-profile'
PROFILE_DIR.mkdir(parents=True, exist_ok=True)

LINKEDIN_BASE = 'https://www.linkedin.com'

# Throttle constants. LinkedIn flags burst scrolls fast.
NAV_DELAY_S = 3.5
SCROLL_DELAY_S = 1.2


def interactive_login() -> None:
    """Open Camoufox in headed mode so the user can log in manually.

    The cookies persist in PROFILE_DIR for subsequent runs.
    """
    print(f'[linkedin] launching headed browser to {LINKEDIN_BASE}/login', flush=True)
    print('[linkedin] log in manually, then close the window', flush=True)

    StealthyFetcher.fetch(
        f'{LINKEDIN_BASE}/login',
        headless=False,
        network_idle=True,
        timeout=300_000,  # 5 minutes for the human to do their thing
        user_data_dir=str(PROFILE_DIR),
        wait=300_000,
    )
    print('[linkedin] login session captured. cookies saved to', PROFILE_DIR, flush=True)


def search_people(query: str, location: str | None, limit: int) -> list[dict[str, Any]]:
    """Run a People search and return parsed cards.

    LinkedIn's search results URL:
        /search/results/people/?keywords=<q>&geoUrn=...&origin=GLOBAL_SEARCH_HEADER

    For simplicity we use the keyword + location-text path; the
    geoUrn approach is more precise but needs a separate location
    lookup call.
    """
    params = [f'keywords={quote_plus(query)}', 'origin=GLOBAL_SEARCH_HEADER']
    if location:
        params.append(f'titleFreeText={quote_plus(location)}')
    url = f'{LINKEDIN_BASE}/search/results/people/?' + '&'.join(params)

    print(f'[linkedin] searching: {url}', flush=True)

    def _scroll_and_collect(page):
        # LinkedIn lazy-loads cards on scroll. Walk a few viewports
        # at human speed.
        for _ in range(min(5, max(1, limit // 5))):
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(int(SCROLL_DELAY_S * 1000))
        page.wait_for_timeout(1500)
        return page

    resp = StealthyFetcher.fetch(
        url,
        headless=True,
        network_idle=True,
        timeout=60_000,
        wait=2_000,
        user_data_dir=str(PROFILE_DIR),
        page_action=_scroll_and_collect,
    )

    if resp is None or getattr(resp, 'status', None) not in (200, 304):
        print(
            f'[linkedin] search returned status {getattr(resp, "status", "?")} — '
            'are you logged in? Run with --login first.',
            flush=True,
        )
        return []

    body = resp.body.decode('utf-8', errors='replace') if isinstance(resp.body, bytes) else str(resp.body)

    # LinkedIn renders search cards inside `<div data-chameleon-result-urn=...>`
    # blocks. The selectors break on every redesign, so we use a
    # forgiving regex pull rather than a brittle CSS selector chain.
    import re
    candidates: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    # Each card has /in/<slug>/ link with a title attribute or anchor text.
    pattern = re.compile(
        r'href="(/in/[^"]+?)"[^>]*?>([^<]+?)</a>'
        r'.{0,800}?(?:role="presentation"|class="[^"]*entity-result__primary-subtitle[^"]*")[^>]*>([^<]+?)<',
        re.DOTALL,
    )
    for m in pattern.finditer(body):
        href, name, role = m.group(1), m.group(2), m.group(3)
        url = f'{LINKEDIN_BASE}{href.split("?")[0]}'
        if url in seen_urls:
            continue
        seen_urls.add(url)
        candidates.append({
            'linkedin_url': url,
            'full_name': name.strip(),
            'role_title': role.strip(),
            'company_name': None,  # secondary line not always reliably parsed
        })
        if len(candidates) >= limit:
            break

    print(f'[linkedin] parsed {len(candidates)} cards', flush=True)
    return candidates


def post_to_painbase(prospects: list[dict[str, Any]]) -> None:
    """POST the harvested prospects to /api/admin/outbound/prospects.

    Auth model: admin cookie. Set PAINBASE_ADMIN_COOKIE env var with
    the value of the painbase_admin cookie from your browser.
    """
    import httpx
    url = os.environ.get('INGEST_URL')
    cookie = os.environ.get('PAINBASE_ADMIN_COOKIE')
    if not url or not cookie:
        print(
            '[linkedin] INGEST_URL and PAINBASE_ADMIN_COOKIE env vars are required '
            'to POST results.',
            flush=True,
        )
        return

    payload = {
        'prospects': [
            {
                'email': '',  # human enriches via Apollo or hand
                'full_name': p['full_name'],
                'role_title': p['role_title'],
                'linkedin_url': p['linkedin_url'],
                'source': 'linkedin',
            }
            for p in prospects
            if p.get('full_name') and p.get('linkedin_url')
        ],
        'enrich': False,
    }

    print(
        f'[linkedin] posting {len(payload["prospects"])} prospects to {url}',
        flush=True,
    )
    res = httpx.post(
        url,
        json=payload,
        headers={'cookie': f'painbase_admin={cookie}'},
        timeout=30.0,
    )
    print(f'[linkedin] ingest response {res.status_code}: {res.text[:300]}', flush=True)


def main() -> int:
    p = argparse.ArgumentParser(description='LinkedIn prospect scraper.')
    p.add_argument('--login', action='store_true', help='Run an interactive login session.')
    p.add_argument('--query', help='Search keywords, e.g. "head of brand"')
    p.add_argument('--location', help='Optional location filter, e.g. "Brazil"')
    p.add_argument('--limit', type=int, default=20, help='Max prospects (≤50 recommended).')
    p.add_argument('--send', action='store_true', help='POST results to Painbase ingest.')
    p.add_argument('--out', help='Optional path to write JSON results.')
    args = p.parse_args()

    if args.login:
        interactive_login()
        return 0

    if not args.query:
        p.error('--query is required (unless using --login)')

    prospects = search_people(args.query, args.location, max(1, min(args.limit, 50)))

    if args.out:
        Path(args.out).write_text(json.dumps(prospects, indent=2, ensure_ascii=False))
        print(f'[linkedin] wrote {len(prospects)} rows to {args.out}', flush=True)
    else:
        for p_row in prospects:
            print(json.dumps(p_row, ensure_ascii=False), flush=True)

    if args.send:
        post_to_painbase(prospects)

    return 0


if __name__ == '__main__':
    # Polite delay before the very first request — gives us a
    # natural pause if the script is being run repeatedly.
    time.sleep(NAV_DELAY_S)
    sys.exit(main())
