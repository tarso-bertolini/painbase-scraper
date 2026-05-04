"""Shared types — mirrors lib/types.ts in the main Painbase repo.

Keep this in sync with `lib/types.ts → Post` and the validator in
`app/api/ingest/raw-scrapes/route.ts → isIncomingScrape`. The ingest
endpoint will reject rows that don't match.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class ScrapedPost:
    """One scraped post, ready for POST to /api/ingest/raw-scrapes."""

    source: str  # 'reclameaqui' | 'reddit' | 'appstore' | 'googleplay' | etc.
    title: str
    content: str | None
    url: str | None
    subreddit: str | None  # reused as company/brand slot
    upvotes: int
    comments_count: int
    author: str | None
    created_at: str  # ISO 8601

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
