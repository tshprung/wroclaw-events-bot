"""Print whether wroclaw.pl/go miejsce listings include application/ld+json."""
from __future__ import annotations

import sys
import urllib.request

urls = sys.argv[1:] or [
    "https://www.wroclaw.pl/go/wydarzenia?miejsce=915-vertigo-jazz-club",
    "https://www.wroclaw.pl/go/wydarzenia?miejsce=140-stary-klasztor",
    "https://www.wroclaw.pl/go/wydarzenia?miejsce=127-impart-centrum-aktualnych-zdarzen",
]

for u in urls:
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            h = r.read().decode("utf-8", "replace")
        ok = "application/ld+json" in h
        print(("OK " if ok else "NO "), len(h), u)
    except Exception as e:
        print("ERR", e, u)
