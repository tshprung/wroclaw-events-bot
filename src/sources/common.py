from __future__ import annotations

import logging
from dataclasses import dataclass
import time
from urllib.parse import urljoin

import requests
import urllib3
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchResult:
    status_code: int
    text: str
    final_url: str


def fetch_url(
    session: requests.Session,
    url: str,
    *,
    timeout: tuple = (5, 20),
    verify: bool | str = True,
) -> FetchResult:

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
    }
    if verify is False:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = session.get(
                url,
                timeout=timeout,
                headers=headers,
                allow_redirects=True,
                verify=verify,
            )

            return FetchResult(status_code=r.status_code, text=r.text, final_url=str(r.url))
        except requests.exceptions.RequestException as e:
            last_exc = e
            # small exponential backoff with jitterless simplicity
            time.sleep(0.6 * (2**attempt))
    assert last_exc is not None
    raise last_exc


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def extract_links(
    base_url: str,
    html: str,
    *,
    selector: str = "a[href]",
    limit: int = 200,
    allow_empty_text: bool = False,
) -> list[tuple[str, str]]:
    s = soup(html)
    out: list[tuple[str, str]] = []
    for a in s.select(selector):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        abs_url = urljoin(base_url, href)
        text = " ".join(a.get_text(" ", strip=True).split())
        if not text and not allow_empty_text:
            continue
        out.append((text, abs_url))
        if len(out) >= limit:
            break
    return out

