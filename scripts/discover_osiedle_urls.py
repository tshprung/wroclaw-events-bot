"""One-off helper: list osiedle.wroc.pl district paths from the public site."""
from __future__ import annotations

import re
import ssl
import urllib.request

URL = "https://osiedle.wroc.pl/"
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
html = urllib.request.urlopen(req, timeout=30, context=ctx).read().decode("utf-8", "replace")
# /index.php/slug or index.php/slug
slugs = sorted(
    set(
        re.findall(
            r"/index\.php/([a-z0-9-]{4,80})(?:/|\?|\"|'|>|#|\s|$)",
            html,
            re.I,
        )
    )
)
# Drop obvious non-district slugs
skip = {"component", "option", "task", "view", "itemid", "wyloguj", "logowanie"}
slugs = [s for s in slugs if s not in skip and not s.startswith("http")]
for s in slugs:
    print(f"https://osiedle.wroc.pl/index.php/{s}")
