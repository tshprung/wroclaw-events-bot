from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from typing import Callable
from urllib.parse import quote, unquote, urlparse

from dateutil import parser as dtparser
from dateutil import tz as dttz
from rapidfuzz.fuzz import token_sort_ratio

from ..models import Event, Source
from .common import extract_links, soup


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
        "in your pocket",
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

# Stock / wallpaper marketplaces often pass the loose “long path” generic heuristic.
_STOCK_OR_MEDIA_MARKETPLACE = re.compile(
    r"(picfair\.com|shutterstock|alamy\.com|dreamstime\.|depositphotos|"
    r"istockphoto|gettyimages|123rf\.|bigstockphoto|pexels\.com|unsplash\.com)",
    re.I,
)

# Off-site links allowed for generic scrapers when they look like tickets/events (not stock photos).
_TICKET_OR_EVENT_NETLOCS = (
    "meetup.com",
    "eventbrite.",
    "facebook.com",
    "fb.com",
    "ebilet.pl",
    "bilety24.pl",
    "going.pl",
    "kupbilet.",
    "sklep.polsat",
    "ticketmaster",
    "eventim",
    "biletomat",
)


def _netloc_key(url: str) -> str:
    h = (urlparse(url).netloc or "").lower().split("@")[-1].split(":")[0]
    if h.startswith("www."):
        h = h[4:]
    return h


def _generic_external_allowed(url: str) -> bool:
    p = urlparse(url)
    blob = f"{p.path or ''}?{p.query or ''}".lower()
    if _EVENT_PATH_HINTS.search(blob):
        return True
    netloc = (p.netloc or "").lower()
    return any(x in netloc for x in _TICKET_OR_EVENT_NETLOCS)


def _generic_link_respects_source_scope(source_page_url: str, link_url: str) -> bool:
    """Keep same-site links or plausible ticket/event deep links; drop random external SEO/shops."""
    if _STOCK_OR_MEDIA_MARKETPLACE.search(urlparse(link_url).netloc or ""):
        return False
    if _netloc_key(source_page_url) == _netloc_key(link_url):
        return True
    return _generic_external_allowed(link_url)


def _clean(s: str) -> str:
    return _SPACE.sub(" ", (s or "").strip())


def _fold_match(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").casefold())
    return "".join(c for c in s if not unicodedata.combining(c))


def _wroclaw_go_ld_rows(html: str) -> list[tuple[str, datetime, str | None]]:
    """schema.org Event rows from application/ld+json (gives real startDate)."""
    tzinfo = dttz.gettz("Europe/Warsaw") or dttz.tzlocal()
    rows: list[tuple[str, datetime, str | None]] = []
    for script in soup(html).select('script[type="application/ld+json"]'):
        raw = (script.string or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        objs = data if isinstance(data, list) else [data]
        for item in objs:
            if not isinstance(item, dict):
                continue
            typ = item.get("@type")
            is_event = typ == "Event" or (isinstance(typ, list) and "Event" in typ)
            if not is_event:
                continue
            name = _clean(item.get("name") or "")
            sd = item.get("startDate")
            if not name or not sd:
                continue
            try:
                st = dtparser.isoparse(str(sd).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if st.tzinfo is None:
                st = st.replace(tzinfo=tzinfo)
            else:
                st = st.astimezone(tzinfo)
            loc = item.get("location")
            venue = None
            if isinstance(loc, dict):
                venue = _clean(loc.get("name") or "") or None
            rows.append((name, st, venue))
    return rows


def _anchor_card_title(anchor_text: str) -> str:
    t = _clean(anchor_text)
    md = _LEADING_DMY_ANCHOR.match(t)
    if md:
        return _clean(md.group("rest"))
    wm = _WROCLAW_GO_WHEN.search(t)
    if wm:
        return _clean(t[: wm.start()].strip())
    return t


def _dedupe_prefer_real_go_url(events: list[Event]) -> list[Event]:
    """Prefer real /go/.../id slug URLs over synthetic listing#evt fragment keys."""
    best: dict[tuple[str, str], Event] = {}
    for e in events:
        sk = e.start_at.isoformat() if e.start_at else ""
        key = (_fold_match(e.title), sk)
        cur = best.get(key)
        if cur is None:
            best[key] = e
        elif "#evt-" in cur.url and "#evt-" not in e.url:
            best[key] = e
        elif "#evt-" not in cur.url:
            best[key] = cur
        else:
            best[key] = e
    return list(best.values())


def _go_synthetic_detail_url(listing_base: str, name: str, st: datetime) -> str:
    """Static HTML often lacks permalinks; fragment keeps URL unique for dedupe and messages."""
    q = quote(f"{name}|{st.isoformat()}"[:220], safe="")
    return f"{listing_base.rstrip('/')}#evt-{q}"


def _best_url_for_ld_name(ld_name: str, cand: list[tuple[str, str]], used_urls: set[str]) -> str | None:
    best_u: str | None = None
    best_sc = 0
    fn = _fold_match(ld_name)
    for text, url in cand:
        if url in used_urls:
            continue
        scores: list[int] = []
        if text:
            at = _anchor_card_title(text)
            if at:
                scores.append(token_sort_ratio(fn, _fold_match(at)))
        slug_t = _slug_title_from_go_url(url)
        if slug_t:
            scores.append(token_sort_ratio(fn, _fold_match(slug_t)))
        if not scores:
            continue
        sc = max(scores)
        if sc > best_sc:
            best_sc = sc
            best_u = url
    return best_u if best_sc >= 74 else None


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
        if not _generic_link_respects_source_scope(source.url, url):
            continue
        key = (text, url)
        if key in seen:
            continue
        seen.add(key)
        out.append(Event(source_id=source.id, title=_clean(text), start_at=None, venue=None, url=url))
    return out


# Detail URLs: /go/wydarzenia/<kategoria>/<id>-<slug> (numeric id). Category-only paths
# are /go/wydarzenia/kino etc. with no id segment.
_GO_EVENT_PATH = re.compile(r"/go/wydarzenia/[^/]+/\d+[-a-z0-9]", re.IGNORECASE)
_GO_URL_SLUG = re.compile(r"/go/wydarzenia/[^/]+/\d+-(.+)$", re.IGNORECASE)


def _slug_title_from_go_url(url: str) -> str:
    path = unquote(urlparse(url).path or "")
    m = _GO_URL_SLUG.search(path)
    if not m:
        return ""
    return _clean(m.group(1).replace("-", " "))


def parse_wroclaw_go(source: Source, html: str) -> list[Event]:
    # Cards are hydrated in JSON-LD (startDate); visible HTML often has only a few <a> rows.
    links = extract_links(
        source.url,
        html,
        selector="a[href*='/go/wydarzenia/']",
        limit=400,
        allow_empty_text=True,
    )
    cand = [(t, u) for t, u in links if _GO_EVENT_PATH.search(u) and (t or _slug_title_from_go_url(u))]
    rows = _wroclaw_go_ld_rows(html)
    used_urls: set[str] = set()
    out: list[Event] = []
    for name, st, ven in rows:
        hit = _best_url_for_ld_name(name, cand, used_urls)
        if hit:
            used_urls.add(hit)
            url = hit
        else:
            url = _go_synthetic_detail_url(source.url, name, st)
        out.append(
            Event(
                source_id=source.id,
                title=name,
                start_at=st,
                venue=ven,
                url=url,
                raw_date_text=None,
            )
        )
    for text, url in cand:
        if url in used_urls:
            continue
        ev = _parse_wroclaw_go_anchor(source.id, text, url)
        if ev.title:
            out.append(ev)
    return _dedupe_prefer_real_go_url(out)


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


# "09.04.2026 Nazwa wydarzenia …" (current wroclaw.pl/go card text)
_LEADING_DMY_ANCHOR = re.compile(
    r"^(?P<dmy>\d{1,2}\.\d{1,2}\.(?:\d{4}|\d{2}))\s+(?P<rest>.+)$",
)


def _parse_wroclaw_go_anchor(source_id: str, text: str, url: str) -> Event:
    text = _clean(text)
    title = text
    when_txt = None
    venue = None
    md = _LEADING_DMY_ANCHOR.match(text)
    if md:
        dmy = _clean(md.group("dmy"))
        rest = _clean(md.group("rest"))
        title = rest
        when_txt = dmy
        tmo = re.search(r"\b(o\s*\d{1,2}\s*[:.]\s*\d{2})\b", rest, re.I)
        if tmo:
            when_txt = _clean(f"{dmy} {tmo.group(1)}")
            tail_venue = rest[tmo.end() :].strip()
            if tail_venue:
                venue = _clean(tail_venue)
        return Event(source_id=source_id, title=title, start_at=None, venue=venue, url=url, raw_date_text=when_txt)

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

