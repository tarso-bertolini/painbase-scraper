/**
 * Mirrors the `Post` shape from the main Painbase repo
 * (lib/types.ts → Post). Keep these two files in sync — the ingest
 * endpoint expects exactly this shape.
 *
 * `id` is omitted because the server assigns it on insert.
 */
export interface ScrapedPost {
  source: string
  title: string
  content: string | null
  subreddit: string | null
  url: string | null
  upvotes: number
  comments_count: number
  author: string | null
  created_at: string // ISO 8601
}

/**
 * Convenience alias used inside scrapers — same shape, named for
 * clarity in the scrape context.
 */
export type Post = ScrapedPost
