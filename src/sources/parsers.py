from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from typing import Callable
from urllib.parse import quote, unquote, urljoin, urlparse

from dateutil import parser as dtparser
from dateutil import tz as dttz
from rapidfuzz.fuzz import partial_ratio, token_set_ratio, token_sort_ratio

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
# Numeric Facebook event permalinks (www, m., locale paths).
_FB_EVENT_PATH_ID = re.compile(r"/events/(\d{8,20})(?:/|\?|#|$)", re.I)
_FB_EVENT_ABS_IN_HTML = re.compile(
    r"https?://(?:www\.|m\.|[a-z]{2}(?:-[a-z]{2})?\.)?facebook\.com/events/(\d{8,20})(?:/|\?|#|\"|'|$)",
    re.I,
)


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


# Detail URLs: /go/wydarzenia/<kategoria>/<id>-<slug> (numeric id).
_GO_EVENT_PATH = re.compile(r"/go/wydarzenia/[^/]+/\d+[-a-z0-9]", re.IGNORECASE)
_GO_URL_SLUG = re.compile(r"/go/wydarzenia/[^/]+/\d+-(.+)$", re.IGNORECASE)


def _slug_title_from_go_url(url: str) -> str:
    path = unquote(urlparse(url).path or "")
    m = _GO_URL_SLUG.search(path)
    if not m:
        return ""
    return _clean(m.group(1).replace("-", " "))


# Also appears inside HTML/JSON payloads as href="wydarzenia/<kategoria>/<id>-<slug>" (no /go prefix).
# Slug may contain "+" (e.g. "dla-dzieci-6+") or other chars — do not restrict to [a-z0-9-].
_WRO_RAW_REL_DETAIL = re.compile(
    r"wydarzenia/([a-z0-9_-]+)/(\d+-[^/\s\"'<>]+)",
    re.IGNORECASE,
)


def _wroclaw_go_site_origin(listing_url: str) -> str:
    p = urlparse(listing_url)
    return f"{p.scheme}://{p.netloc}"


def _wroclaw_go_extra_detail_urls(html: str, listing_url: str) -> list[str]:
    """Many listing pages embed canonical paths only in JSON/router blobs, not in <a href>."""
    origin = _wroclaw_go_site_origin(listing_url)
    found: set[str] = set()
    for cat, rest in _WRO_RAW_REL_DETAIL.findall(html):
        abs_u = urljoin(origin, f"/go/wydarzenia/{cat}/{rest}").rstrip("/")
        if _GO_EVENT_PATH.search(urlparse(abs_u).path or ""):
            found.add(abs_u)
    for m in re.finditer(r"/go/wydarzenia/[a-z0-9_-]+/\d+-[^/\s\"'<>]+", html, re.IGNORECASE):
        abs_u = urljoin(origin, m.group(0)).rstrip("/")
        if _GO_EVENT_PATH.search(urlparse(abs_u).path or ""):
            found.add(abs_u)
    return list(found)


def _abs_http_url(u: str | None, base_page: str) -> str | None:
    if not u or not isinstance(u, str):
        return None
    u = u.strip()
    if not u or u.startswith(("javascript:", "mailto:", "tel:", "#")):
        return None
    if u.startswith("//"):
        u = "https:" + u
    if not u.startswith(("http://", "https://")):
        u = urljoin(base_page, u)
    if not u.startswith(("http://", "https://")):
        return None
    return u


def _path_looks_like_event_detail_url(url: str) -> bool:
    """True for /wydarzenia/concrete-slug etc., not bare org homepages or listing roots."""
    raw = (urlparse(url).path or "").strip("/")
    if not raw:
        return False
    parts = [x for x in raw.split("/") if x]
    pl = "/" + "/".join(parts).lower() + "/"
    if "wydarzen" in pl and len(parts) >= 2:
        return True
    if _EVENT_PATH_HINTS.search(pl):
        return True
    netloc = (urlparse(url).netloc or "").lower()
    if "meetup." in netloc and "/events/" in pl:
        return True
    return False


def _ld_json_event_fallback_url(item: dict, listing_url: str) -> str | None:
    """Use schema.org url / offers / sameAs / organizer when wroclaw.pl omits <a> permalinks."""

    def take(candidate: str | None, *, strict_organizer: bool = False) -> str | None:
        u = _abs_http_url(candidate, listing_url)
        if not u:
            return None
        path = urlparse(u).path or ""
        if _GO_EVENT_PATH.search(path):
            return u
        if strict_organizer:
            return u if _path_looks_like_event_detail_url(u) else None
        if _path_looks_like_event_detail_url(u):
            return u
        if _generic_external_allowed(u):
            return u
        return None

    u = take(item.get("url"))
    if u:
        return u
    sa = item.get("sameAs")
    if isinstance(sa, str):
        u = take(sa)
        if u:
            return u
    elif isinstance(sa, list):
        for x in sa:
            if isinstance(x, str):
                u = take(x)
                if u:
                    return u
    off = item.get("offers")
    if isinstance(off, dict):
        u = take(off.get("url"))
        if u:
            return u
    org = item.get("organizer")
    if isinstance(org, dict):
        u = take(org.get("url"), strict_organizer=True)
        if u:
            return u
    return None


def _wroclaw_go_ld_rows(html: str, listing_url: str) -> list[tuple[str, datetime, str | None, str | None]]:
    """schema.org Event rows from application/ld+json (gives real startDate)."""
    tzinfo = dttz.gettz("Europe/Warsaw") or dttz.tzlocal()
    rows: list[tuple[str, datetime, str | None, str | None]] = []
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
            fb = _ld_json_event_fallback_url(item, listing_url)
            rows.append((name, st, venue, fb))
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


def _go_synthetic_detail_url(listing_base: str, name: str, st: datetime) -> str:
    """Static HTML often lacks permalinks; fragment keeps URL unique for dedupe and messages."""
    q = quote(f"{name}|{st.isoformat()}"[:220], safe="")
    return f"{listing_base.rstrip('/')}#evt-{q}"


def _normalize_title_for_slug_fuzz(title: str) -> str:
    """LD titles often use |, [], age markers — strip for comparing to URL slug tokens."""
    t = _clean(title)
    t = re.sub(r"[\[\]|]+", " ", t)
    t = re.sub(r"\([^)]*\)", " ", t)
    t = t.replace("+", " plus ")
    return _clean(t)


def _best_url_for_ld_name(ld_name: str, cand: list[tuple[str, str]], used_urls: set[str]) -> str | None:
    best_u: str | None = None
    best_sc = 0
    fn = _fold_match(_normalize_title_for_slug_fuzz(ld_name))
    for text, url in cand:
        if url in used_urls:
            continue
        scores: list[int] = []
        if text:
            at = _anchor_card_title(text)
            if at:
                am = _fold_match(at)
                scores.append(token_sort_ratio(fn, am))
                scores.append(token_set_ratio(fn, am))
        slug_t = _slug_title_from_go_url(url)
        if slug_t:
            sm = _fold_match(slug_t)
            scores.append(token_sort_ratio(fn, sm))
            scores.append(token_set_ratio(fn, sm))
            if len(slug_t) >= 8:
                scores.append(partial_ratio(fn, sm))
        if not scores:
            continue
        sc = max(scores)
        if sc > best_sc:
            best_sc = sc
            best_u = url
    return best_u if best_sc >= 72 else None


def _wroclaw_go_url_rank(url: str) -> int:
    if "#evt-" in url:
        return 0
    path = urlparse(url).path or ""
    if _GO_EVENT_PATH.search(path):
        return 4
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return 0
    if _path_looks_like_event_detail_url(url):
        return 3
    if _generic_external_allowed(url):
        return 2
    return 0


def _dedupe_prefer_real_go_url(events: list[Event]) -> list[Event]:
    """Prefer wroclaw.pl /go/.../id slugs, then concrete external pages, over #evt fragments."""
    best: dict[tuple[str, str], Event] = {}
    for e in events:
        sk = e.start_at.isoformat() if e.start_at else ""
        key = (_fold_match(e.title), sk)
        cur = best.get(key)
        if cur is None:
            best[key] = e
        elif _wroclaw_go_url_rank(e.url) > _wroclaw_go_url_rank(cur.url):
            best[key] = e
        else:
            best[key] = cur
    return list(best.values())


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


def _canonical_facebook_event_url(url: str) -> str | None:
    if not url or url.startswith(("javascript:", "mailto:", "tel:", "#")):
        return None
    u = url.strip()
    if u.startswith("//"):
        u = "https:" + u
    if not u.startswith("http"):
        return None
    p = urlparse(u)
    net = (p.netloc or "").lower()
    if "facebook.com" not in net and "fb.com" not in net:
        return None
    path = unquote(p.path or "")
    m = _FB_EVENT_PATH_ID.search(path)
    if not m:
        return None
    return f"https://www.facebook.com/events/{m.group(1)}/"


def parse_facebook_event_search(source: Source, html: str) -> list[Event]:
    """Public /events/search listings: collect permalinks; dates/venues stay on Facebook."""
    ids: set[str] = set()
    titles: dict[str, str] = {}

    def _good_anchor_title(t: str) -> bool:
        t2 = _clean(t)
        if not t2 or len(t2) > 200:
            return False
        if _JUNK_TITLE_RE.match(t2):
            return False
        if t2.lower() in _JUNK_TITLE_EQ:
            return False
        return True

    def note(url: str, anchor_text: str = "") -> None:
        can = _canonical_facebook_event_url(url)
        if not can:
            return
        eid = can.rstrip("/").rsplit("/", 1)[-1]
        if not eid.isdigit():
            return
        ids.add(eid)
        if _good_anchor_title(anchor_text):
            t2 = _clean(anchor_text)
            prev = titles.get(eid, "")
            if len(t2) > len(prev):
                titles[eid] = t2

    for text, url in extract_links(
        source.url,
        html,
        selector='a[href*="/events/"]',
        limit=350,
        allow_empty_text=True,
    ):
        note(url, text)

    for m in _FB_EVENT_ABS_IN_HTML.finditer(html):
        ids.add(m.group(1))

    out: list[Event] = []
    for eid in sorted(ids, key=int):
        u = f"https://www.facebook.com/events/{eid}/"
        title = titles.get(eid) or "Wydarzenie (Facebook)"
        out.append(Event(source_id=source.id, title=title, start_at=None, venue=None, url=u))
    return out


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


def parse_wroclaw_go(source: Source, html: str) -> list[Event]:
    # Cards are hydrated in JSON-LD (startDate); visible HTML often has only a few <a> rows.
    links = extract_links(
        source.url,
        html,
        selector="a[href*='/go/wydarzenia/']",
        limit=400,
        allow_empty_text=True,
    )
    by_u: dict[str, tuple[str, str]] = {}
    for t, u in links:
        u = (u or "").strip().rstrip("/")
        if not u or not _GO_EVENT_PATH.search(u) or not (t or _slug_title_from_go_url(u)):
            continue
        by_u[u] = (t, u)
    for u in _wroclaw_go_extra_detail_urls(html, source.url):
        by_u.setdefault(u, ("", u))
    cand = list(by_u.values())
    rows = _wroclaw_go_ld_rows(html, source.url)
    used_urls: set[str] = set()
    out: list[Event] = []
    for name, st, ven, ld_fb in rows:
        hit = _best_url_for_ld_name(name, cand, used_urls)
        if hit:
            used_urls.add(hit)
            url = hit
        elif ld_fb:
            url = ld_fb
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
        query = (p.query or "").lower()
        if "kinonh.pl" not in host:
            continue
        if path.rstrip("/") in ("", "/"):
            continue
        if "/resetujpass" in path:
            continue
        # Ticket purchase / repertory showtimes are not "events" for this bot.
        if path.rstrip("/") == "/bilet.s" or "eventid=" in query:
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
        if re.search(r"\bkup\s+bilet\b", t, re.IGNORECASE):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(Event(source_id=source.id, title=t, start_at=None, venue="Kino Nowe Horyzonty", url=url))
    return out


def parser_for_kind(kind: str) -> Callable[[Source, str], list[Event]]:
    return {
        "generic_links": parse_generic_links,
        "facebook_event_search": parse_facebook_event_search,
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

