from __future__ import annotations

import logging
import os
from dataclasses import dataclass
import time
from urllib.parse import urljoin, urlparse, urlunparse

import requests
import urllib3
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


def _http_timeout() -> tuple[float, float]:
    """(connect, read) seconds; override with HTTP_FETCH_TIMEOUT=connect,read e.g. 15,45."""
    raw = (os.environ.get("HTTP_FETCH_TIMEOUT") or "").strip()
    if raw:
        parts = [p.strip() for p in raw.replace(" ", ",").split(",") if p.strip()]
        try:
            if len(parts) >= 2:
                return (float(parts[0]), float(parts[1]))
            if len(parts) == 1:
                v = float(parts[0])
                return (v, max(v * 2.5, 30.0))
        except ValueError:
            pass
    return (10.0, 35.0)


@dataclass(frozen=True)
class FetchResult:
    status_code: int
    text: str
    final_url: str


def _browser_ua() -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )


def _facebook_search_headers(*, referer: str) -> dict[str, str]:
    return {
        "User-Agent": _browser_ua(),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "max-age=0",
        "Referer": referer,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin" if "facebook.com" in referer else "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


def _m_facebook_equivalent(url: str) -> str:
    p = urlparse(url)
    host = (p.netloc or "").lower()
    if not host.endswith("facebook.com"):
        return url
    if host == "m.facebook.com":
        return url
    return urlunparse(p._replace(netloc="m.facebook.com"))


def fetch_facebook_event_search(
    session: requests.Session,
    url: str,
    *,
    timeout: tuple[float, float] | None = None,
    verify: bool | str = True,
) -> FetchResult:
    """Facebook often returns 400 to bare clients; try desktop- and mobile-style requests."""
    if verify is False:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    if timeout is None:
        timeout = _http_timeout()

    attempts: list[tuple[str, dict[str, str]]] = [
        (url, _facebook_search_headers(referer="https://www.facebook.com/")),
        (_m_facebook_equivalent(url), _facebook_search_headers(referer="https://m.facebook.com/")),
    ]

    last_exc: Exception | None = None
    last_result: FetchResult | None = None
    for req_url, headers in attempts:
        for attempt in range(3):
            try:
                r = session.get(
                    req_url,
                    timeout=timeout,
                    headers=headers,
                    allow_redirects=True,
                    verify=verify,
                )
                last_result = FetchResult(status_code=r.status_code, text=r.text, final_url=str(r.url))
                if r.status_code < 400:
                    return last_result
                break
            except requests.exceptions.RequestException as e:
                last_exc = e
                time.sleep(0.6 * (2**attempt))
    if last_result is not None:
        return last_result
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("fetch_facebook_event_search: empty attempts")


def fetch_url(
    session: requests.Session,
    url: str,
    *,
    timeout: tuple[float, float] | None = None,
    verify: bool | str = True,
) -> FetchResult:

    headers = {
        "User-Agent": _browser_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
    }
    if verify is False:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if timeout is None:
        timeout = _http_timeout()

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

