import type { ScrapedPost } from './types.js'

/**
 * POSTs scraped posts back to the Painbase ingest API in batches.
 *
 * Env contract:
 *   INGEST_URL    — full URL of the ingest endpoint, e.g.
 *                   https://painbase.com.br/api/ingest/raw-scrapes
 *   INGEST_TOKEN  — bearer token; matches the value set on the
 *                   Painbase API side. Stored as a GHA repo secret.
 *
 * Pass `--dry-run` (via process.argv) on any scraper script to skip
 * the network call and dump the would-be payload to stdout instead.
 * Useful when iterating on selectors without spamming the ingest API.
 */

interface IngestResult {
  inserted: number
  duplicates: number
  skipped: number
  errors?: string[]
}

const BATCH_SIZE = 50 // matches the Painbase API's per-request cap

export async function postScrapesToIngest(
  posts: ScrapedPost[],
  options: {
    source: string
    runId?: string
    dryRun?: boolean
  },
): Promise<IngestResult> {
  if (posts.length === 0) {
    return { inserted: 0, duplicates: 0, skipped: 0 }
  }

  const dryRun = options.dryRun ?? process.argv.includes('--dry-run')
  if (dryRun) {
    console.log(JSON.stringify({ source: options.source, count: posts.length, posts }, null, 2))
    return { inserted: 0, duplicates: 0, skipped: posts.length }
  }

  const url = process.env.INGEST_URL
  const token = process.env.INGEST_TOKEN
  if (!url || !token) {
    throw new Error(
      'INGEST_URL and INGEST_TOKEN env vars are required when not running with --dry-run',
    )
  }

  const totals: IngestResult = { inserted: 0, duplicates: 0, skipped: 0, errors: [] }

  // Chunk the post array so a single huge scrape doesn't blow past
  // any payload size limits and so we get progress visibility.
  for (let i = 0; i < posts.length; i += BATCH_SIZE) {
    const batch = posts.slice(i, i + BATCH_SIZE)
    const res = await fetch(url, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        authorization: `Bearer ${token}`,
        'x-painbase-scraper-source': options.source,
        ...(options.runId ? { 'x-painbase-run-id': options.runId } : {}),
      },
      body: JSON.stringify({ scrapes: batch }),
    })

    if (!res.ok) {
      const text = await res.text().catch(() => '<unreadable>')
      const msg = `Ingest batch ${i / BATCH_SIZE} failed (${res.status}): ${text.slice(0, 240)}`
      totals.errors!.push(msg)
      console.error(msg)
      continue
    }

    const data = (await res.json().catch(() => ({}))) as Partial<IngestResult>
    totals.inserted += data.inserted ?? 0
    totals.duplicates += data.duplicates ?? 0
    totals.skipped += data.skipped ?? 0
  }

  return totals
}
