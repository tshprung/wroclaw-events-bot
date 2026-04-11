"""Emit generic_links YAML blocks for osiedle.wroc.pl district landing pages."""
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
slugs = sorted(
    set(
        re.findall(
            r"/index\.php/([a-z0-9-]{4,80})(?:/|\?|\"|'|>|#|\s|$)",
            html,
            re.I,
        )
    )
)

def keep(slug: str) -> bool:
    if slug in {"global-kontakt", "obsluga-wsparcie-osiedli", "rady-osiedli"}:
        return False
    if re.match(r"^\d", slug):
        return False
    if re.match(r"^\d{4}-\d{2}-\d{2}", slug):
        return False
    return True


slugs = [s for s in slugs if keep(s)]

print("  # --- osiedle.wroc.pl: wszystkie osiedla z menu (regeneruj przy zmianie strony) ---")
print("  - id: osiedle_wroc_pl")
print("    name: osiedle.wroc.pl (home)")
print("    url: https://osiedle.wroc.pl/")
print("    kind: generic_links")
print("    verify_ssl: false")
print("    enabled: true")
print()

for slug in slugs:
    sid = "osiedle_wroc_" + slug.replace("-", "_")
    print(f"  - id: {sid}")
    print(f"    name: osiedle.wroc.pl / {slug}")
    print(f"    url: https://osiedle.wroc.pl/index.php/{slug}")
    print("    kind: generic_links")
    print("    verify_ssl: false")
    print("    enabled: true")
    print()
