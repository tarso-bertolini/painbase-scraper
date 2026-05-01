/**
 * Reclame Aqui — Playwright scraper.
 *
 * RA's `iosite` JSON endpoints are gated behind Cloudflare's "Just
 * a moment..." JS challenge. A vanilla `fetch` from a server gets
 * 403'd on every path. This script:
 *
 *   1. Launches stealth-patched Chromium
 *   2. Loads `https://www.reclameaqui.com.br/empresa/{slug}/` to
 *      pass CF's challenge once. The challenge cookie persists
 *      across the page session.
 *   3. Calls the JSON endpoint via `page.evaluate(fetch(...))` so
 *      the request inherits the page's CF cookies.
 *   4. Walks the paginated complaint list, normalizes to the
 *      Painbase Post shape, and POSTs to the ingest API.
 *
 * Run locally:
 *   pnpm tsx scripts/scrape/reclameaqui.ts nubank --dry-run
 *   pnpm tsx scripts/scrape/reclameaqui.ts nubank cashu c6bank   # batch
 *
 * Run on GHA: see .github/workflows/scrape-reclameaqui.yml.
 */
import { chromium } from 'playwright-extra'
// @ts-expect-error — no types ship with the stealth plugin
import StealthPlugin from 'puppeteer-extra-plugin-stealth'
import type { ScrapedPost } from '../../lib/types.js'
import { postScrapesToIngest } from '../../lib/ingest-client.js'

chromium.use(StealthPlugin())

const RA_BASE = 'https://www.reclameaqui.com.br'
const PAGE_SIZE = 30
const MAX_PAGES_PER_BRAND = 3 // ~90 complaints/brand/run is plenty
const NAV_DELAY_MS = 2000

interface RAComplaint {
  id?: string | number
  title?: string
  description?: string
  status?: string
  url?: string
  created?: string
  creationDate?: string
  city?: string
  state?: string
  userFirstName?: string
  userName?: string
}

interface RAComplaintsResponse {
  total?: number
  complains?: { data?: RAComplaint[] } | RAComplaint[]
  data?: RAComplaint[]
}

function normalize(c: RAComplaint, slug: string): ScrapedPost | null {
  const title = String(c.title || '').trim()
  if (!title) return null

  const url = c.url
    ? c.url.startsWith('http')
      ? c.url
      : `${RA_BASE}${c.url}`
    : c.id
      ? `${RA_BASE}/empresa/${slug}/relato/${c.id}/`
      : null
  if (!url) return null

  const created = c.creationDate || c.created
  return {
    source: 'reclameaqui',
    title: title.slice(0, 300),
    content: String(c.description || '').trim() || title,
    url,
    subreddit: slug, // Painbase reuses the subreddit slot for company name
    upvotes: 0,
    comments_count: 0,
    author: c.userFirstName || c.userName || null,
    created_at: created ? new Date(created).toISOString() : new Date().toISOString(),
  }
}

async function scrapeBrand(slug: string): Promise<ScrapedPost[]> {
  console.log(`[ra:${slug}] launching browser...`)
  const browser = await chromium.launch({
    headless: true,
    args: ['--disable-blink-features=AutomationControlled'],
  })

  // A human-ish locale + timezone makes CF's behaviour score
  // visibly happier. Brazilian locale lines up with the target audience.
  const context = await browser.newContext({
    locale: 'pt-BR',
    timezoneId: 'America/Sao_Paulo',
    viewport: { width: 1366, height: 768 },
    userAgent:
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
  })
  const page = await context.newPage()

  const out: ScrapedPost[] = []
  const seen = new Set<string>()

  try {
    // Step 1 — visit the company page to clear CF challenge.
    const companyUrl = `${RA_BASE}/empresa/${encodeURIComponent(slug)}/`
    console.log(`[ra:${slug}] resolving CF challenge via ${companyUrl}`)
    await page.goto(companyUrl, { waitUntil: 'domcontentloaded', timeout: 45_000 })
    // Heuristic wait for the CF interstitial to vanish — the stealth
    // plugin handles most cases automatically, but networks vary.
    await page.waitForTimeout(NAV_DELAY_MS)

    // If the page title is still "Just a moment..." we lost the
    // bypass. Bail and try the next brand instead of looping forever.
    const title = await page.title()
    if (/just a moment/i.test(title)) {
      console.warn(`[ra:${slug}] CF challenge not bypassed — bailing`)
      return out
    }

    // Step 2 — paginate through the JSON endpoint, inheriting the
    // CF cookie from the page context.
    for (let pageNo = 0; pageNo < MAX_PAGES_PER_BRAND; pageNo++) {
      const apiUrl = `https://iosite.reclameaqui.com.br/raichu-io-site-v1/companies/shortname/${encodeURIComponent(
        slug,
      )}/complains?count=${PAGE_SIZE}&start=${pageNo * PAGE_SIZE}&filter=created_desc`

      const json = (await page.evaluate(async (u) => {
        const r = await fetch(u, {
          credentials: 'include',
          headers: {
            accept: 'application/json, text/plain, */*',
            'accept-language': 'pt-BR,pt;q=0.9',
          },
        })
        if (!r.ok) return { __error: r.status }
        return r.json()
      }, apiUrl)) as RAComplaintsResponse | { __error: number }

      if ('__error' in json) {
        console.warn(`[ra:${slug}] page ${pageNo} → HTTP ${json.__error}, stopping`)
        break
      }

      const items: RAComplaint[] =
        (Array.isArray(json.complains) ? json.complains : json.complains?.data) ??
        json.data ??
        []
      if (!items.length) break

      let added = 0
      for (const c of items) {
        const post = normalize(c, slug)
        if (!post) continue
        const key = post.url ?? post.title
        if (seen.has(key)) continue
        seen.add(key)
        out.push(post)
        added++
      }
      console.log(`[ra:${slug}] page ${pageNo}: ${items.length} fetched, ${added} new`)

      if (items.length < PAGE_SIZE) break
      await page.waitForTimeout(1000) // throttle between pages
    }
  } finally {
    await context.close()
    await browser.close()
  }

  console.log(`[ra:${slug}] done — ${out.length} complaints`)
  return out
}

async function main() {
  // CLI args = brand slugs. Default set covers the BR fintechs we
  // care about for the v1 launch; can be overridden in the workflow
  // via inputs or by passing args to the script directly.
  const args = process.argv.slice(2).filter((a) => !a.startsWith('--'))
  const brands =
    args.length > 0
      ? args
      : ['nubank', 'cashu', 'c6-bank', 'banco-inter', 'picpay', 'mercado-pago']

  const runId = process.env.GITHUB_RUN_ID || new Date().toISOString()
  console.log(`[ra] run ${runId} — brands: ${brands.join(', ')}`)

  const all: ScrapedPost[] = []
  for (const slug of brands) {
    try {
      const posts = await scrapeBrand(slug)
      all.push(...posts)
    } catch (err) {
      console.error(`[ra:${slug}] failed:`, err instanceof Error ? err.message : err)
    }
  }

  console.log(`[ra] total scraped: ${all.length}`)

  if (all.length > 0) {
    const result = await postScrapesToIngest(all, {
      source: 'reclameaqui',
      runId,
    })
    console.log(`[ra] ingest result:`, result)
  } else {
    console.log('[ra] nothing to ingest')
  }
}

main().catch((err) => {
  console.error('[ra] fatal:', err)
  process.exit(1)
})
