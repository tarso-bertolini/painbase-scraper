"""Probe whether Scrapling's capture_xhr intercepts RA's iosite calls."""
import json
import sys
from scrapling.fetchers import StealthyFetcher


def trigger_lazy_load(page):
    for _ in range(6):
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(1200)
    page.wait_for_timeout(2000)
    return page


def main() -> int:
    resp = StealthyFetcher.fetch(
        "https://www.reclameaqui.com.br/empresa/nubank/lista-reclamacoes/",
        headless=True,
        network_idle=True,
        timeout=90000,
        wait=3000,
        page_action=trigger_lazy_load,
        capture_xhr=r"iosite\.reclameaqui\.com\.br",
    )

    print(f"status: {resp.status}", flush=True)
    print(f"captured_xhr count: {len(resp.captured_xhr)}", flush=True)

    for i, x in enumerate(resp.captured_xhr[:8]):
        url = getattr(x, "url", "?")
        status = getattr(x, "status", "?")
        print(f"\n=== XHR #{i} ===", flush=True)
        print(f"  url:    {url}", flush=True)
        print(f"  status: {status}", flush=True)

        body = getattr(x, "body", None)
        if body is None:
            text = getattr(x, "text", "")
            body = text.encode("utf-8") if text else b""
        if isinstance(body, bytes):
            body_str = body.decode("utf-8", errors="replace")
        else:
            body_str = str(body)

        if not body_str:
            print(f"  body:   <empty>", flush=True)
            continue

        try:
            data = json.loads(body_str)
            if isinstance(data, dict):
                print(f"  json keys: {list(data.keys())[:6]}", flush=True)
                complains = data.get("complains")
                if isinstance(complains, dict):
                    inner = complains.get("data")
                    if isinstance(inner, list):
                        print(f"  complains.data count: {len(inner)}", flush=True)
                        if inner:
                            print(f"  first title: {inner[0].get('title', '?')[:120]}", flush=True)
                elif isinstance(complains, list):
                    print(f"  complains list count: {len(complains)}", flush=True)
            elif isinstance(data, list):
                print(f"  list length: {len(data)}", flush=True)
        except Exception:
            print(f"  body[0:200]: {body_str[:200]}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
