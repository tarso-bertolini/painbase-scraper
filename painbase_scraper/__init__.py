"""painbase_scraper — shared utilities for the Scrapling-based fleet.

The package exposes:
    ScrapedPost   — the row shape the Painbase /api/ingest/raw-scrapes
                    endpoint accepts. Mirrors lib/types.ts in the main
                    Painbase repo; keep both files in sync.

    post_scrapes_to_ingest — POSTs a list of ScrapedPost objects to
                             the ingest endpoint in 50-row batches.
                             Honours INGEST_URL + INGEST_TOKEN env
                             vars and `--dry-run` on argv.
"""
from .types import ScrapedPost
from .ingest import post_scrapes_to_ingest

__all__ = ["ScrapedPost", "post_scrapes_to_ingest"]
