"""Inspect what's actually inside the captured XHR's `data` field."""
import json
import sys
from scrapling.fetchers import StealthyFetcher


def trigger_lazy_load(page):
    for _ in range(4):
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(1000)
    page.wait_for_timeout(2000)
    return page


def main() -> int:
    resp = StealthyFetcher.fetch(
        "https://www.reclameaqui.com.br/empresa/nubank/lista-reclamacoes/",
        headless=True,
        network_idle=True,
        timeout=90000,
        wait=2000,
        page_action=trigger_lazy_load,
        capture_xhr=r"iosite\.reclameaqui\.com\.br/raichu-io-site-v1/companyshowcase/company/\d+/items",
        disable_resources=True,
        solve_cloudflare=True,
    )

    print(f"status: {resp.status}", flush=True)
    print(f"captured XHRs: {len(resp.captured_xhr)}", flush=True)

    for i, xhr in enumerate(resp.captured_xhr):
        url = getattr(xhr, "url", "?")
        body = getattr(xhr, "body", None)
        if body is None:
            text = getattr(xhr, "text", "")
            body = text.encode("utf-8") if text else b""
        if isinstance(body, bytes):
            body_str = body.decode("utf-8", errors="replace")
        else:
            body_str = str(body)

        print(f"\n=== XHR #{i} ({len(body_str)} bytes) ===", flush=True)
        print(f"url: {url}", flush=True)
        if not body_str:
            print(f"  body: <empty>", flush=True)
            continue

        try:
            data = json.loads(body_str)
        except json.JSONDecodeError:
            print(f"  body[0:200]: {body_str[:200]}", flush=True)
            continue

        if isinstance(data, dict):
            print(f"  top-level keys: {list(data.keys())}", flush=True)
            for k, v in data.items():
                if isinstance(v, list):
                    print(f"  .{k} (list len={len(v)})", flush=True)
                    if v:
                        sample = v[0]
                        if isinstance(sample, dict):
                            print(f"    sample[0] keys: {list(sample.keys())[:12]}", flush=True)
                            # Show a few field values to identify the data shape
                            for field in ("title", "id", "complainTitle", "name", "description", "creationDate", "url", "status"):
                                if field in sample:
                                    val = str(sample[field])[:140]
                                    print(f"      .{field}: {val}", flush=True)
                        else:
                            print(f"    sample[0]: {str(sample)[:140]}", flush=True)
                elif isinstance(v, dict):
                    print(f"  .{k} (dict, keys: {list(v.keys())[:6]})", flush=True)
                else:
                    print(f"  .{k}: {str(v)[:80]}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
