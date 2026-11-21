#!/usr/bin/env python3
"""Diagnose a refused download: fetch each address with a few header sets and log only the status.

Synergy's site answered GitHub's runners with HTTP 403 (2026-11-21) while Horizon Power's worked.
This logs status, content type and size for each try, nothing of the page itself.
"""

from __future__ import annotations

from pathlib import Path
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_eme_retailers as eme  # noqa: E402

URLS = [
    "https://www.synergy.net.au/-/media/Files/PDF-Library/DEBS_Pricing_schedule.pdf",
    "https://www.synergy.net.au/Your-home/Energy-plans/Home-Plan-A1",
    "https://www.horizonpower.com.au/utilities/pricing/",
]
BROWSER_ACCEPT = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}
TRIES = {
    "1 current": {"User-Agent": eme.USER_AGENT},
    "2 honest UA + Accept": {"User-Agent": eme.USER_AGENT, **BROWSER_ACCEPT},
    "3 browser UA": {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"), **BROWSER_ACCEPT},
}


def main() -> int:
    for url in URLS:
        for name, headers in TRIES.items():
            request = urllib.request.Request(url, headers=headers)  # noqa: S310 (fixed https addresses)
            try:
                with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                    body = response.read()
                    result = f"{response.status} {response.headers.get('Content-Type')} {len(body)} bytes"
            except urllib.error.HTTPError as err:
                result = f"{err.code} {err.reason} (server: {err.headers.get('Server')})"
            except OSError as err:
                result = f"failed: {err}"
            print(f"FETCH {url} | try {name}: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
