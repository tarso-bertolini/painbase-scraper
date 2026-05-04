"""Ingest client — POSTs scraped posts to the Painbase API in batches.

Env contract (matches lib/ingest-client.ts):
    INGEST_URL    full URL of the ingest endpoint
                  e.g. https://painbase.com.br/api/ingest/raw-scrapes
    INGEST_TOKEN  bearer token; matches the value set on the Painbase
                  side. Stored as a GHA repo secret.

Pass --dry-run on argv to skip the network call and dump JSON to
stdout instead — useful when iterating on selectors locally.
"""
from __future__ import annotations
import json
import os
import sys
from dataclasses import dataclass, asdict
from typing import Iterable

import httpx

from .types import ScrapedPost

# Matches the per-request cap on the Painbase API side
# (app/api/ingest/raw-scrapes/route.ts → MAX_BATCH = 100).
# We chunk at 50 to stay well under and to give us per-batch
# observability when debugging.
BATCH_SIZE = 50


@dataclass
class IngestResult:
    inserted: int = 0
    duplicates: int = 0
    skipped: int = 0
    errors: list[str] | None = None


def _is_dry_run() -> bool:
    """`--dry-run` on argv OR DRY_RUN env var truthy."""
    if "--dry-run" in sys.argv:
        return True
    val = os.environ.get("DRY_RUN", "").lower()
    return val in ("1", "true", "yes")


def post_scrapes_to_ingest(
    posts: Iterable[ScrapedPost],
    *,
    source: str,
    run_id: str | None = None,
    dry_run: bool | None = None,
) -> IngestResult:
    """POST `posts` to the ingest endpoint, returning ingest stats.

    `source` is forwarded as the `x-painbase-scraper-source` header so
    spike-attribution in production logs is easy. `run_id` is included
    when present (we set it from GITHUB_RUN_ID inside workflows).
    """
    posts = list(posts)
    if not posts:
        return IngestResult()

    do_dry = _is_dry_run() if dry_run is None else dry_run
    if do_dry:
        print(json.dumps(
            {"source": source, "count": len(posts),
             "posts": [p.to_dict() for p in posts]},
            indent=2, ensure_ascii=False
        ))
        return IngestResult(skipped=len(posts))

    url = os.environ.get("INGEST_URL")
    token = os.environ.get("INGEST_TOKEN")
    if not url or not token:
        raise RuntimeError(
            "INGEST_URL and INGEST_TOKEN env vars are required when "
            "not running with --dry-run"
        )

    totals = IngestResult(errors=[])

    with httpx.Client(timeout=30.0) as client:
        # Chunk so a single huge scrape doesn't blow past the server
        # cap and so we get per-batch progress.
        for i in range(0, len(posts), BATCH_SIZE):
            batch = posts[i:i + BATCH_SIZE]
            headers = {
                "content-type": "application/json",
                "authorization": f"Bearer {token}",
                "x-painbase-scraper-source": source,
            }
            if run_id:
                headers["x-painbase-run-id"] = run_id

            try:
                resp = client.post(
                    url,
                    headers=headers,
                    json={"scrapes": [p.to_dict() for p in batch]},
                )
            except httpx.RequestError as e:
                msg = f"Ingest batch {i // BATCH_SIZE} network error: {e}"
                print(msg, file=sys.stderr)
                totals.errors.append(msg)  # type: ignore[union-attr]
                continue

            if resp.status_code >= 400:
                body = resp.text[:240] if resp.text else "<empty>"
                msg = (f"Ingest batch {i // BATCH_SIZE} failed "
                       f"({resp.status_code}): {body}")
                print(msg, file=sys.stderr)
                totals.errors.append(msg)  # type: ignore[union-attr]
                continue

            try:
                data = resp.json()
            except ValueError:
                data = {}

            totals.inserted += int(data.get("inserted", 0))
            totals.duplicates += int(data.get("duplicates", 0))
            totals.skipped += int(data.get("skipped", 0))

    return totals
