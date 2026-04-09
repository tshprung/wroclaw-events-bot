from __future__ import annotations

import re
from typing import Callable
from urllib.parse import urlparse

from ..models import Event, Source
from .common import extract_links


_SPACE = re.compile(r"\s+")

# Titles that are almost always UI chrome (not event names).
_JUNK_TITLE_RE = re.compile(
    r"^(A|A\+|A\+\+|0|\d{1,3}|dzisiaj|jutro|więcej|więcej dat|menu|home|"
    r"zaloguj|zgoda|polski|english|google[\s-]?plus|impart)$",
    re.I,
)

_JUNK_TITLE_EQ = frozenset(
    {
        "o nas",
        "program",
        "projekty",
        "archiwum",
        "wydawnictwo",
        "dostępność",
        "warsztaty",
        "o firmie",
        "grotowski.net",
        "jerzy grotowski",
        "e-mail: impart@impart.pl",
    }
)

_EVENT_PATH_HINTS = re.compile(
    r"""(/go/|wydarzen|/event|/events/|spektakl|koncert|/film|repertuar|bilet|seans|
        kino|aktualno|imprez|kalendarz|ebilet|ticket|spektaklu|koncercie)""",
    re.I | re.VERBOSE,
)

_NAV_ONLY_PATH_RE = re.compile(
    r"/(o-nas|program|kalendarium|projekty|archiwum|wydawnictwo|"
    r"warsztaty|deklaracja|o-firmie|strona-glowna|wydarzenia)\s*/?\s*$",
    re.I,
)


def _clean(s: str) -> str:
    return _SPACE.sub(" ", (s or "").strip())


def _generic_link_keeps(text: str, url: str) -> bool:
    """Drop nav, auth, JS, and other non-content links from generic scrapes."""
    t = _clean(text)
    if not t or len(t) > 220:
        return False
    low = t.lower()
    low_u = url.strip().lower()
    if low_u.startswith(("javascript:", "mailto:", "tel:", "#")):
        return False
    if "void(" in low_u or low_u.startswith("javascript:"):
        return False
    netloc = (urlparse(url).netloc or "").lower()
    raw_path = urlparse(url).path or ""
    path = raw_path.lower()
    if "google." in netloc and "search" in path:
        return False
    if "encyklopedia" in path or "resetujpass" in path:
        return False
    if _JUNK_TITLE_RE.match(t):
        return False
    if low in _JUNK_TITLE_EQ:
        return False
    if "nie pamiętasz hasła" in low:
        return False
    if re.match(r"^(so|nd|pn|wt|śr|cz|sw|pt)\b", low):
        return False
    if _NAV_ONLY_PATH_RE.search(raw_path):
        return False
    if len(t) <= 2 and not _EVENT_PATH_HINTS.search(path):
        return False
    return _likely_event_like_url(url)


def _likely_event_like_url(url: str) -> bool:
    p = urlparse(url)
    path = p.path or ""
    pl = path.lower()
    if pl in ("", "/"):
        return False
    if re.search(r"/wydarzenia/?$", pl):
        return False
    if _EVENT_PATH_HINTS.search(pl):
        return True
    if "/go/" in pl:
        return True
    segs = [s for s in pl.split("/") if s]
    if len(pl) >= 28 and len(segs) >= 2:
        return True
    if len(segs) == 1 and len(segs[0]) >= 36:
        return True
    return False


def parse_generic_links(source: Source, html: str) -> list[Event]:
    # Generic fallback: create low-fidelity “events” from prominent links.
    # This is meant as scaffolding; source-specific parsers should replace it.
    out: list[Event] = []
    links = extract_links(source.url, html, selector="a[href]", limit=50)
    seen = set()
    for text, url in links:
        if not _generic_link_keeps(text, url):
            continue
        key = (text, url)
        if key in seen:
            continue
        seen.add(key)
        out.append(Event(source_id=source.id, title=_clean(text), start_at=None, venue=None, url=url))
    return out


_GO_EVENT_PATH = re.compile(r"/go/wydarzenia/wydarzenia/[^/]+/\d", re.IGNORECASE)


def parse_wroclaw_go(source: Source, html: str) -> list[Event]:
    # Real event cards use /go/wydarzenia/wydarzenia/<kategoria>/<id>-<slug> — not nav/footer
    # links that only mention /go/wydarzenia/. Scanning the first N generic <a href> hits mostly menus.
    links = extract_links(
        source.url,
        html,
        selector="a[href*='/go/wydarzenia/wydarzenia/']",
        limit=200,
    )
    out: list[Event] = []
    for text, url in links:
        if not _GO_EVENT_PATH.search(url):
            continue
        out.append(_parse_wroclaw_go_anchor(source.id, text, url))
    # Filter out empties
    return [e for e in out if e.title]


# Start of the "when" chunk on wroclaw.pl/go listing anchors (often: Title WHEN ...optional venue...).
_WROCLAW_GO_WHEN = re.compile(
    r"\b(Dziś|Dzisiaj|Jutro|Pojutrze|"
    r"Sobota|Niedziela|Poniedziałek|Wtorek|Środa|Czwartek|Piątek)\b",
    re.IGNORECASE,
)
# After the when-clause, venue often follows "o HH:MM".
_WROCLAW_GO_AFTER_TIME_VENUE = re.compile(
    r"^(.+?\bo\s*\d{1,2}\s*[:.]\s*\d{2})(?:\s+(.+))?$",
    re.IGNORECASE,
)


def _parse_wroclaw_go_anchor(source_id: str, text: str, url: str) -> Event:
    text = _clean(text)
    title = text
    when_txt = None
    venue = None
    wm = _WROCLAW_GO_WHEN.search(text)
    if wm:
        title = _clean(text[: wm.start()].strip())
        tail = _clean(text[wm.start() :].strip())
        when_txt = tail
        mtv = _WROCLAW_GO_AFTER_TIME_VENUE.match(tail)
        if mtv:
            when_txt = _clean(mtv.group(1).strip())
            if mtv.group(2):
                venue = _clean(mtv.group(2).strip())
    return Event(source_id=source_id, title=title, start_at=None, venue=venue, url=url, raw_date_text=when_txt)


def parse_wroclawguide_calendar(source: Source, html: str) -> list[Event]:
    # The calendar page includes repeating blocks: day/month + time + venue + title.
    # We keep it simple by capturing heading links as event URLs, and embed time/venue into raw_date_text when possible.
    return parse_generic_links(source, html)


def parse_meetup_find(source: Source, html: str) -> list[Event]:
    # Meetup list includes per-event links; keep generic extraction but limited.
    links = extract_links(source.url, html, selector="a[href*='/events/']", limit=80)
    out: list[Event] = []
    seen = set()
    for text, url in links:
        if "/events/" not in url:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(Event(source_id=source.id, title=_clean(text), start_at=None, venue=None, url=url))
    return out


def parse_hala_stulecia(source: Source, html: str) -> list[Event]:
    links = extract_links(source.url, html, selector="a[href*='/wydarzenie/']", limit=80)
    out: list[Event] = []
    seen = set()
    for text, url in links:
        if "/wydarzenie/" not in url:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(Event(source_id=source.id, title=_clean(text), start_at=None, venue="Hala Stulecia / WCK", url=url))
    return out


def parse_tarczynski_arena(source: Source, html: str) -> list[Event]:
    # Calendar page includes event titles in headings; extract all links and keep those that look like event slugs if present.
    return parse_generic_links(source, html)


def parse_nfm_repertuar(source: Source, html: str) -> list[Event]:
    # NFM includes links to /component/nfmcalendar/event/<id>
    links = extract_links(source.url, html, selector="a[href*='/component/nfmcalendar/event/']", limit=120)
    out: list[Event] = []
    seen = set()
    for text, url in links:
        if "/component/nfmcalendar/event/" not in url:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(Event(source_id=source.id, title=_clean(text), start_at=None, venue="NFM", url=url))
    return out


def parse_grotowski_wydarzenia(source: Source, html: str) -> list[Event]:
    links = extract_links(source.url, html, selector="a[href]", limit=120)
    out: list[Event] = []
    seen: set[str] = set()
    for text, url in links:
        ul = url.strip().lower()
        if ul.startswith(("javascript:", "mailto:", "tel:")) or "void(" in ul:
            continue
        p = urlparse(url)
        if "grotowski-institute.pl" not in (p.netloc or "").lower():
            continue
        parts = [x for x in (p.path or "").split("/") if x]
        ok = False
        if "wydarzenia" in parts:
            i = parts.index("wydarzenia")
            ok = i + 1 < len(parts)
        if not ok and "projekty" in parts:
            i = parts.index("projekty")
            ok = i + 1 < len(parts)
        if not ok:
            continue
        if url in seen:
            continue
        seen.add(url)
        t = _clean(text)
        if not t:
            continue
        out.append(Event(source_id=source.id, title=t, start_at=None, venue=None, url=url))
    return out


def parse_kino_nh(source: Source, html: str) -> list[Event]:
    links = extract_links(source.url, html, selector="a[href]", limit=120)
    out: list[Event] = []
    seen: set[str] = set()
    for text, url in links:
        ul = url.strip().lower()
        if ul.startswith(("javascript:", "mailto:", "tel:")) or "void(" in ul:
            continue
        p = urlparse(url)
        host = (p.netloc or "").lower()
        path = (p.path or "").lower()
        if "kinonh.pl" not in host:
            continue
        if path.rstrip("/") in ("", "/"):
            continue
        if "/resetujpass" in path:
            continue
        looks = any(
            x in path
            for x in (
                "/film",
                "/movie",
                "seans",
                "repertuar",
                "wydarzen",
                "bilet",
                "projekcja",
            )
        )
        if not looks and not (path.count("/") >= 2 and len(path) >= 22):
            continue
        t = _clean(text)
        if not t or _JUNK_TITLE_RE.match(t):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(Event(source_id=source.id, title=t, start_at=None, venue="Kino Nowe Horyzonty", url=url))
    return out


def parser_for_kind(kind: str) -> Callable[[Source, str], list[Event]]:
    return {
        "generic_links": parse_generic_links,
        "wroclaw_go": parse_wroclaw_go,
        "hala_stulecia": parse_hala_stulecia,
        "tarczynski_arena": parse_tarczynski_arena,
        "nfm_repertuar": parse_nfm_repertuar,
        "meetup_find": parse_meetup_find,
        "wroclawguide_calendar": parse_wroclawguide_calendar,
        "grotowski_wydarzenia": parse_grotowski_wydarzenia,
        "kino_nh": parse_kino_nh,
        # place-holders:
        "wydarzenia_wroclaw": parse_generic_links,
        "pik": parse_generic_links,
        "crossweb": parse_generic_links,
        "ebilet_city": parse_generic_links,
        "wroclaw_travel_calendar": parse_generic_links,
    }.get(kind, parse_generic_links)

