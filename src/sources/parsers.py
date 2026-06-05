from __future__ import annotations

import html as html_module
import json
import re
import unicodedata
from datetime import datetime, tzinfo
from typing import Callable
from urllib.parse import quote, unquote, urljoin, urlparse, urlunparse

import requests
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
        "kup bilet",
        "szczegóły",
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


def _wroclaw_go_detail_id_from_source_url(listing_url: str) -> str | None:
    """If `listing_url` is a single-event /go/wydarzenia/.../id-slug page, return numeric id."""
    path = unquote(urlparse(listing_url).path or "")
    m = re.search(r"/go/wydarzenia/[^/]+/(\d+)-", path, re.I)
    return m.group(1) if m else None


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
    """Legacy: listing-page #evt- fragments (no longer emitted; kept for tests / callers)."""
    q = quote(f"{name}|{st.isoformat()}"[:220], safe="")
    return f"{listing_base.rstrip('/')}#evt-{q}"


def _wroclaw_go_listing_fragment_url(url: str) -> bool:
    """True if URL is a wroclaw.pl/go calendar hash, not /wydarzenia/.../id-slug."""
    return "#evt-" in (url or "")


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
    if "ebilet.pl" in netloc and "/klasyka/koncert" in path:
        return False
    if "ebilet.pl" in netloc and "/miasto/" in path:
        return False
    if "wteatrw.pl" in netloc.replace("www.", ""):
        # Combined-ticket product pages (not a single dated performance); ticket anchor dupes.
        if "bilet_laczony" in path:
            return False
        if "#to_tickets" in low_u:
            return False
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
    if "osiedle.wroc.pl" in netloc.replace("www.", ""):
        if not _osiedle_wroc_pl_link_keeps(raw_path, t):
            return False
    if "wydarzenia.wroclaw.pl" in netloc.replace("www.", ""):
        # Category hubs (/koncerty/, /spektakle/) match _EVENT_PATH_HINTS via "koncert"/"spektakl"; blog is not events.
        pln = path.replace("//", "/")
        if "/blog/" in pln:
            return False
        segs = [s for s in raw_path.strip("/").split("/") if s]
        if len(segs) < 2:
            return False
    if "inyourpocket.com" in netloc.replace("www.", ""):
        if _inyourpocket_event_slug(raw_path) is None:
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


# osiedle.wroc.pl — only Joomla articles with a numeric id; drop district index pages and admin/news noise.
_OSIEDLE_ARTICLE = re.compile(r"/index\.php/(?:[^/]+/)?\d{3,}-", re.I)
_OSIEDLE_PATH_NOISE = (
    "fundusz",
    "dofinansowanie",
    "senioralny",
    "ksef",
    "samorzad",
    "kontakt",
    "dokumenty",
    "galeria",
    "deklaracja",
    "zarzad-osiedla",
    "rada-osiedla",
    "statut",
    "dyzury",
    "wazne-kontakty",
    "informator-osiedlowy",
    "-komisji-",
    "-komisja-",
    "konsultacji-spolecznych",
    "wcrs-",
    "obsluga-wsparcie",
    "komunikaty",
    "planu-ogolnego-miasta",
    "planu-ogoln",
)
_OSIEDLE_TITLE_FOLD_NOISE = (
    "fundusz",
    "dofinansowanie",
    "ksef",
    "deklaracja dostepnosci",
    "zarzad osiedla",
    "rada osiedla",
    "statut",
    "dyzury",
    "galeria",
    "dokumenty",
    "kontakt",
    "wazne kontakty",
    "informator osiedlowy",
    "zebranie komisji",
    "konsultacji spolecznych",
    "obsluga osiedli",
)


def _osiedle_wroc_pl_link_keeps(path: str, title: str) -> bool:
    pl = (path or "").lower()
    if not _OSIEDLE_ARTICLE.search(path or ""):
        return False
    for frag in _OSIEDLE_PATH_NOISE:
        if frag in pl:
            return False
    folded = _fold_match(title or "")
    if folded.strip(" .…") == "wiecej":
        return False
    for frag in _OSIEDLE_TITLE_FOLD_NOISE:
        if frag in folded:
            return False
    return True


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


_PIK_EMBEDDED_GO_DETAIL = re.compile(
    r"https://(?:www\.)?wroclaw\.pl/go/wydarzenia/[a-z0-9_-]+/\d+-[^\"'\s<>#]+",
    re.I,
)


def parse_pik(source: Source, html: str) -> list[Event]:
    """PIK: generic nav links plus any wroclaw.pl/go event permalinks embedded in the HTML."""
    out = parse_generic_links(source, html)
    seen: set[str] = {e.url for e in out}
    for m in _PIK_EMBEDDED_GO_DETAIL.finditer(html):
        u = m.group(0).split("#")[0].rstrip("/.")
        path = unquote(urlparse(u).path or "")
        if not _GO_EVENT_PATH.search(path):
            continue
        if u in seen:
            continue
        seen.add(u)
        slug_t = _slug_title_from_go_url(u)
        title = _clean(slug_t) if slug_t else "Wydarzenie (wroclaw.pl/go)"
        out.append(Event(source_id=source.id, title=title, start_at=None, venue=None, url=u))
    return out


_KRAJOWNIK_WROCLAW_DETAIL = re.compile(r"^/wroclaw/wydarzenia/[^/?#]+-\d+/?$", re.I)


def parse_krajownik_wroclaw_wydarzenia(source: Source, html: str) -> list[Event]:
    def _looks_like_when(text: str) -> bool:
        t = _clean(text)
        if not t:
            return False
        if not re.search(r"\b\d{1,2}\s+[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ]+\s+\d{4}\b", t, re.UNICODE):
            return False
        return True

    links = extract_links(
        source.url,
        html,
        selector='a[href*="/wroclaw/wydarzenia/"]',
        limit=650,
        allow_empty_text=True,
    )
    by_url: dict[str, dict[str, str]] = {}
    for text, url in links:
        p = urlparse(url)
        host = (p.netloc or "").lower()
        if "krajownik.pl" not in host:
            continue
        path = unquote(p.path or "")
        if not _KRAJOWNIK_WROCLAW_DETAIL.match(path):
            continue
        clean_url = f"{p.scheme}://{p.netloc}{path}".rstrip("/")
        t = _clean(text)
        if not t:
            continue
        rec = by_url.setdefault(clean_url, {})
        low = t.casefold()
        if low == "wrocław" or low == "wroclaw":
            continue
        if _looks_like_when(t):
            prev = rec.get("when") or ""
            if len(t) > len(prev):
                rec["when"] = t
            continue
        prev_title = rec.get("title") or ""
        if len(t) > len(prev_title):
            rec["title"] = t
        prev_venue = rec.get("venue") or ""
        if len(t) >= 6 and t != rec.get("title", ""):
            if any(x in low for x in ("ul.", "ulica", "plac", "rynek", "bulwar", "strefa", "centrum")) or any(
                ch.isdigit() for ch in t
            ):
                if len(t) > len(prev_venue):
                    rec["venue"] = t

    out: list[Event] = []
    for u, rec in by_url.items():
        out.append(
            Event(
                source_id=source.id,
                title=rec.get("title") or "Wydarzenie (Krajownik)",
                start_at=None,
                venue=rec.get("venue") or None,
                url=u,
                raw_date_text=rec.get("when") or None,
            )
        )
    return out


_IYP_EVENT_DETAIL_PATH = re.compile(r"^/(?:poland/)?wroclaw/events/([^/]+)/?$", re.I)


def _inyourpocket_event_slug(path: str) -> str | None:
    m = _IYP_EVENT_DETAIL_PATH.match((path or "").strip())
    return m.group(1).lower() if m else None


def _inyourpocket_canonical_event_url(url_or_path: str) -> str | None:
    raw = (url_or_path or "").strip()
    path = urlparse(raw).path if raw.startswith(("http://", "https://")) else raw
    slug = _inyourpocket_event_slug(path)
    if not slug:
        return None
    return f"https://www.inyourpocket.com/wroclaw/events/{slug}"


def parse_inyourpocket_events(source: Source, html: str) -> list[Event]:
    """Only individual event pages under /wroclaw/events/<slug> — not listing hubs or travel articles."""
    tzinfo = dttz.gettz("Europe/Warsaw") or dttz.tzlocal()
    by_url: dict[str, Event] = {}

    def upsert(name: str, url: str, *, start_at: datetime | None, venue: str | None) -> None:
        clean = _inyourpocket_canonical_event_url(url)
        if not clean:
            return
        title = _clean(html_module.unescape(name))
        if not title or _JUNK_TITLE_RE.match(title) or title.lower() in _JUNK_TITLE_EQ:
            return
        ev = Event(
            source_id=source.id,
            title=title,
            start_at=start_at,
            venue=venue,
            url=clean,
            raw_date_text=None,
        )
        prev = by_url.get(clean)
        if prev is None:
            by_url[clean] = ev
            return
        kw: dict = {}
        if prev.start_at is None and start_at is not None:
            kw["start_at"] = start_at
        if not (prev.venue or "").strip() and (venue or "").strip():
            kw["venue"] = venue
        if len(title) > len(prev.title or "") + 2:
            kw["title"] = title
        if kw:
            by_url[clean] = Event(
                source_id=source.id,
                title=kw.get("title", prev.title),
                start_at=kw.get("start_at", prev.start_at),
                venue=kw.get("venue", prev.venue),
                url=clean,
                raw_date_text=None,
            )

    for script in soup(html).select('script[type="application/ld+json"]'):
        raw = (script.string or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        blocks: list[dict] = []
        if isinstance(data, dict):
            blocks.append(data)
            for el in data.get("itemListElement") or []:
                if isinstance(el, dict):
                    blocks.append(el)
        elif isinstance(data, list):
            blocks.extend(x for x in data if isinstance(x, dict))

        for item in blocks:
            typ = item.get("@type")
            is_ev = typ == "Event" or (isinstance(typ, list) and "Event" in typ)
            if not is_ev:
                continue
            name = item.get("name") or ""
            url_u = item.get("url") or ""
            if not name or not url_u:
                continue
            st: datetime | None = None
            sd = item.get("startDate")
            if sd:
                try:
                    st = dtparser.isoparse(str(sd).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    st = None
                if st is not None:
                    if st.tzinfo is None:
                        st = st.replace(tzinfo=tzinfo)
                    else:
                        st = st.astimezone(tzinfo)
            loc = item.get("location")
            venue = None
            if isinstance(loc, dict):
                venue = _clean(loc.get("name") or "") or None
            upsert(name, url_u, start_at=st, venue=venue)

    links = extract_links(source.url, html, selector="a[href]", limit=120)
    for text, url in links:
        clean = _inyourpocket_canonical_event_url(url)
        if not clean:
            continue
        upsert(text, clean, start_at=None, venue=None)

    return list(by_url.values())


def parse_generic_links(source: Source, html: str, *, link_limit: int | None = None) -> list[Event]:
    # Generic fallback: create low-fidelity “events” from prominent links.
    # This is meant as scaffolding; source-specific parsers should replace it.
    out: list[Event] = []
    if link_limit is not None:
        lim = int(link_limit)
    elif source.link_limit is not None:
        lim = int(source.link_limit)
    else:
        lim = 50
    lim = max(1, min(800, lim))
    links = extract_links(source.url, html, selector="a[href]", limit=lim)
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


def parse_ebilet_pl(source: Source, html: str) -> list[Event]:
    """eBilet front page — use JSON-LD to keep real Event rows (then window filters)."""
    out = _parse_ebilet_jsonld(source, html, require_wroclaw=True)
    if out:
        return out
    return _parse_ebilet_embedded_schema(source, html, require_wroclaw=True)


def parse_ebilet_city(source: Source, html: str) -> list[Event]:
    """eBilet city page (e.g. /miasto/wroclaw) — use JSON-LD Event rows."""
    out = _parse_ebilet_jsonld(source, html, require_wroclaw=True)
    if out:
        return out
    return _parse_ebilet_embedded_schema(source, html, require_wroclaw=True)


def _parse_ebilet_jsonld(source: Source, html: str, *, require_wroclaw: bool) -> list[Event]:
    tzinfo = dttz.gettz("Europe/Warsaw") or dttz.tzlocal()
    out: list[Event] = []
    seen: set[str] = set()
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
            url = (item.get("url") or "").strip()
            if not name or not sd or not url:
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
            locality = ""
            if isinstance(loc, dict):
                venue = _clean(loc.get("name") or "") or None
                addr = loc.get("address")
                if isinstance(addr, dict):
                    locality = _clean(addr.get("addressLocality") or "")
            if require_wroclaw:
                if "wroclaw" not in _fold_match(f"{name} {venue or ''} {locality} {url}"):
                    continue

            url = url.split("#")[0].rstrip("/")
            if url in seen:
                continue
            seen.add(url)
            out.append(Event(source_id=source.id, title=name, start_at=st, venue=venue, url=url, raw_date_text=None))
    return out


_EBILET_EMBEDDED_EVENT = re.compile(
    r'"name"\s*:\s*"(?P<name>[^"]{3,200})"\s*,'
    r'[\s\S]{0,900}?"startDate"\s*:\s*"(?P<start>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"'
    r'(?:[\s\S]{0,300}?"endDate"\s*:\s*"(?P<end>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")?'
    r'[\s\S]{0,1400}?"url"\s*:\s*"(?P<url>https?://(?:www\.)?ebilet\.pl/[^"]{5,500})"',
    re.I,
)


def _ebilet_slug_title(url: str) -> str:
    path = unquote(urlparse(url).path or "").strip().rstrip("/")
    leaf = (path.split("/")[-1] if path else "").strip()
    return _clean(leaf.replace("-", " ").replace("_", " ")) or "Wydarzenie (eBilet)"


def _parse_ebilet_embedded_schema(source: Source, html: str, *, require_wroclaw: bool) -> list[Event]:
    tzinfo = dttz.gettz("Europe/Warsaw") or dttz.tzlocal()
    hits: list[tuple[str, datetime, str]] = []  # (url, start_at, folded_locality)
    for m in _EBILET_EMBEDDED_EVENT.finditer(html):
        url = (m.group("url") or "").split("#")[0].rstrip("/")
        sd = m.group("start")
        ed = m.group("end")
        if not url or not sd:
            continue
        # If the card is a date range / multi-city series page, skip it.
        if ed and ed != sd:
            continue
        try:
            st = dtparser.isoparse(sd)
        except (ValueError, TypeError):
            continue
        if st.tzinfo is None:
            st = st.replace(tzinfo=tzinfo)
        else:
            st = st.astimezone(tzinfo)

        # Locality is usually embedded close to the same event object, but not always within the small match span.
        frag = html[m.start() : min(len(html), m.end() + 6000)]
        ml = re.search(r'addressLocality"\s*:\s*"([^"]{2,80})"', frag, re.I)
        if not ml:
            continue
        loc = _fold_match(ml.group(1))
        hits.append((url, st, loc))

    # Drop tour/artist pages where one URL maps to many cities.
    url_locs: dict[str, set[str]] = {}
    for u, _st, loc in hits:
        url_locs.setdefault(u, set()).add(loc)

    out: list[Event] = []
    seen: set[str] = set()
    for u, st, loc in hits:
        if u in seen:
            continue
        seen.add(u)
        locs = url_locs.get(u) or set()
        if len(locs) != 1:
            continue
        if require_wroclaw and "wroclaw" not in locs:
            continue
        out.append(Event(source_id=source.id, title=_ebilet_slug_title(u), start_at=st, venue=None, url=u, raw_date_text=None))
    return out


def parse_nowiny_olesnickie_wydarzenia(source: Source, html: str) -> list[Event]:
    """Nowiny Oleśnickie — label „Wydarzenia”: article links near text mentioning Wrocław (folded)."""
    out: list[Event] = []
    seen: set[str] = set()
    for m in re.finditer(
        r"https://www\.nowinyolesnickie\.pl/20\d{2}/\d{2}/[^\"'\s<>]+\.html",
        html,
        re.I,
    ):
        u = m.group(0).split("#")[0].rstrip("/")
        if u in seen:
            continue
        lo, hi = max(0, m.start() - 800), min(len(html), m.end() + 800)
        if "wroclaw" not in _fold_match(html[lo:hi]):
            continue
        seen.add(u)
        leaf = unquote(urlparse(u).path.rstrip("/").split("/")[-1].replace(".html", ""))
        out.append(
            Event(
                source_id=source.id,
                title=_clean(leaf.replace("-", " ")),
                start_at=None,
                venue=None,
                url=u,
            )
        )
    for e in parse_generic_links(source, html, link_limit=120):
        if e.url in seen:
            continue
        if "wroclaw" not in _fold_match(f"{e.title} {e.url}"):
            continue
        seen.add(e.url)
        out.append(e)
    return out[:100]


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
        if _wroclaw_go_listing_fragment_url(u):
            continue
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
        if hit and _wroclaw_go_listing_fragment_url(hit):
            hit = None
        if hit:
            used_urls.add(hit)
            url = hit
        elif ld_fb and not _wroclaw_go_listing_fragment_url(ld_fb):
            url = ld_fb
        else:
            # No real permalink — skip (avoid posting wszystkie#evt-… listing anchors).
            continue
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
        if _wroclaw_go_listing_fragment_url(url):
            continue
        ev = _parse_wroclaw_go_anchor(source.id, text, url)
        if ev.title:
            out.append(ev)
    out = _dedupe_prefer_real_go_url(out)
    detail_id = _wroclaw_go_detail_id_from_source_url(source.url)
    if detail_id:
        out = [e for e in out if f"/{detail_id}-" in (urlparse(e.url).path or "")]
    return out


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


def _merge_wroclawguide_jsonld_duplicates(
    current: Event,
    incoming: Event,
    *,
    local_tz: tzinfo,
) -> Event:
    """Same event URL can appear in several JSON-LD blocks (e.g. date-only midnight vs real start time)."""
    ca, ib = current.start_at, incoming.start_at
    if ca is None:
        return incoming
    if ib is None:
        return current

    def _is_local_midnight(t: datetime) -> bool:
        lt = t.astimezone(local_tz)
        return lt.hour == 0 and lt.minute == 0

    da, db = ca.astimezone(local_tz).date(), ib.astimezone(local_tz).date()
    if da == db:
        if _is_local_midnight(ib) and not _is_local_midnight(ca):
            return current
        if _is_local_midnight(ca) and not _is_local_midnight(ib):
            return incoming

    if len(incoming.title or "") > len(current.title or "") + 2:
        return incoming
    if not (current.venue or "").strip() and (incoming.venue or "").strip():
        return incoming
    return current if ca <= ib else incoming


def parse_wroclawguide_calendar(source: Source, html: str) -> list[Event]:
    # The calendar HTML embeds many standalone JSON-LD Event blocks (startDate + url). Generic
    # link extraction has no dates, so undated rows bypass EVENT_WINDOW_DAYS.
    local_tz = dttz.gettz("Europe/Warsaw") or dttz.tzlocal()
    by_url: dict[str, Event] = {}
    for script in soup(html).select('script[type="application/ld+json"]'):
        raw = (script.string or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        objs = data.get("@graph") if isinstance(data, dict) and "@graph" in data else None
        if objs is None:
            objs = data if isinstance(data, list) else [data]
        for item in objs:
            if not isinstance(item, dict):
                continue
            typ = item.get("@type")
            is_ev = typ == "Event" or (isinstance(typ, list) and "Event" in typ)
            if not is_ev:
                continue
            name = _clean(html_module.unescape(_clean(item.get("name") or "")))
            sd = item.get("startDate")
            url_u = (item.get("url") or "").strip()
            if not name or not sd or not url_u:
                continue
            lu = url_u.lower()
            if "wroclawguide.com" not in lu or "/events/" not in lu:
                continue
            if "/events-category/" in lu or "event-calendar-wroclaw" in lu:
                continue
            try:
                st = dtparser.isoparse(str(sd).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if st.tzinfo is None:
                st = st.replace(tzinfo=local_tz)
            else:
                st = st.astimezone(local_tz)
            loc = item.get("location")
            venue = None
            if isinstance(loc, dict):
                venue = _clean(loc.get("name") or "") or None
            clean = url_u.split("#")[0].rstrip("/")
            ev = Event(
                source_id=source.id,
                title=name,
                start_at=st,
                venue=venue,
                url=clean,
                raw_date_text=None,
            )
            prev = by_url.get(clean)
            if prev is None:
                by_url[clean] = ev
            else:
                by_url[clean] = _merge_wroclawguide_jsonld_duplicates(prev, ev, local_tz=local_tz)
    out = list(by_url.values())
    if out:
        return out
    lim = source.link_limit if source.link_limit is not None else 300
    return parse_generic_links(source, html, link_limit=lim)


def parse_meetup_find(source: Source, html: str) -> list[Event]:
    # Meetup list includes per-event links; keep generic extraction but limited.
    links = extract_links(source.url, html, selector="a[href*='/events/']", limit=80)
    out: list[Event] = []
    seen = set()
    for text, url in links:
        if "/events/" not in url:
            continue
        # Drop tracking parameters so we don't treat the same event as new.
        # Example: .../events/313562179/?recId=...&searchId=...
        p = urlparse(url)
        if not (p.scheme and p.netloc and p.path):
            continue
        clean_url = f"{p.scheme}://{p.netloc}{p.path}"
        if "/events/" not in clean_url:
            continue
        # Meetup canonical permalinks end with a trailing slash.
        if not clean_url.endswith("/"):
            clean_url += "/"
        if clean_url in seen:
            continue
        seen.add(clean_url)
        out.append(Event(source_id=source.id, title=_clean(text), start_at=None, venue=None, url=clean_url))
    return out


_HALA_LIST_DATE_RANGE = re.compile(
    r"^(\d{1,2}\.\d{1,2}\.\d{4})\s*-\s*(\d{1,2}\.\d{1,2}\.\d{4})\s*$",
)
_HALA_LIST_DATE_TIME_SLASH = re.compile(
    r"^(\d{1,2}\.\d{1,2}\.\d{4})\s*/\s*(\d{1,2}:\d{2})\s*$",
)


def _hala_listing_raw_when_from_time_el(time_el) -> str | None:
    """Turn <time> inner text into a string `event_window._parse_raw_when` understands."""
    if time_el is None:
        return None
    raw = _clean(time_el.get_text(" ", strip=True))
    if not raw:
        return None
    m = _HALA_LIST_DATE_RANGE.match(raw)
    if m:
        # Multi-day block: use first calendar day as the start for window filtering.
        return m.group(1)
    m2 = _HALA_LIST_DATE_TIME_SLASH.match(raw)
    if m2:
        return f"{m2.group(1)} {m2.group(2)}"
    return raw


def parse_hala_stulecia(source: Source, html: str) -> list[Event]:
    # Listing cards include <p class="post-date"><time>DD.MM.YYYY / HH:MM</time> or a date range.
    # Without this, rows are undated and bypass EVENT_WINDOW_DAYS when EVENT_WINDOW_INCLUDE_UNDATED=1.
    s = soup(html)
    out: list[Event] = []
    seen: set[str] = set()
    for art in s.select("div.event_list_big article"):
        a = art.select_one("h2.post-title.entry-title a[href*='/wydarzenie/']")
        if not a or not a.get("href"):
            continue
        href = (a.get("href") or "").strip().rstrip("/")
        if "/wydarzenie/" not in href:
            continue
        if href in seen:
            continue
        title = _clean(a.get_text(" ", strip=True)) or _clean(html_module.unescape((a.get("title") or "").strip()))
        if not title or title.lower() in _JUNK_TITLE_EQ or _JUNK_TITLE_RE.match(title):
            continue
        raw_when = _hala_listing_raw_when_from_time_el(art.select_one("p.post-date time"))
        if not raw_when:
            continue
        seen.add(href)
        out.append(
            Event(
                source_id=source.id,
                title=title,
                start_at=None,
                venue="Hala Stulecia / WCK",
                url=href,
                raw_date_text=raw_when,
            )
        )
    if out:
        return out
    # Markup changed: keep a low-fidelity fallback (undated — may bypass the time window).
    links = extract_links(source.url, html, selector="a[href*='/wydarzenie/']", limit=80)
    for text, url in links:
        if "/wydarzenie/" not in url:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(Event(source_id=source.id, title=_clean(text), start_at=None, venue="Hala Stulecia / WCK", url=url))
    return out


_TA_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
}


def _tarczynski_canonical_event_url(abs_u: str) -> str | None:
    """Map /events/<slug>/… (any ?occurrence=) to https://host/events/<slug>/."""
    try:
        p = urlparse((abs_u or "").strip())
    except ValueError:
        return None
    if "tarczynskiarenawroclaw.pl" not in (p.netloc or "").lower():
        return None
    parts = [unquote(x) for x in (p.path or "").split("/") if x]
    if parts and parts[0].lower() in {"en", "de", "uk"}:
        parts = parts[1:]
    if len(parts) != 2 or parts[0].lower() != "events":
        return None
    slug = parts[1]
    if not slug or slug.lower() in {"events", "page"} or slug.isdigit():
        return None
    path = f"/events/{slug}/"
    return urlunparse(("https", "tarczynskiarenawroclaw.pl", path, "", "", ""))


def _tarczynski_event_page_exists(url: str, *, verify_ssl: bool) -> bool:
    """Drop stale MEC links still embedded in HTML (404 on canonical event URL)."""
    try:
        r = requests.head(
            url,
            timeout=12,
            allow_redirects=True,
            verify=verify_ssl,
            headers=_TA_UA,
        )
        if r.status_code in (405, 501):
            r = requests.get(
                url,
                timeout=12,
                allow_redirects=True,
                verify=verify_ssl,
                headers=_TA_UA,
                stream=True,
            )
            try:
                r.close()
            except Exception:
                pass
        return r.status_code < 400
    except requests.exceptions.RequestException:
        # Network hiccup: keep the row rather than silently dropping real listings.
        return True


def parse_tarczynski_arena(source: Source, html: str) -> list[Event]:
    # MEC embeds past ?occurrence= links; generic link extraction also picks nav noise.
    # Canonicalize to /events/<slug>/ and require HTTP 200 so dead permalinks never post.
    s = soup(html)
    best_title: dict[str, str] = {}
    for a in s.select("a[href*='/events/']"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        abs_u = urljoin(source.url, href)
        canon = _tarczynski_canonical_event_url(abs_u)
        if not canon:
            continue
        title = _clean(a.get_text(" ", strip=True)) or _clean(html_module.unescape((a.get("title") or "").strip()))
        if not title or title.lower() in _JUNK_TITLE_EQ or _JUNK_TITLE_RE.match(title):
            continue
        prev = best_title.get(canon, "")
        if len(title) >= len(prev):
            best_title[canon] = title

    verify = bool(source.verify_ssl)
    out: list[Event] = []
    for url, title in sorted(best_title.items(), key=lambda kv: kv[0]):
        if not _tarczynski_event_page_exists(url, verify_ssl=verify):
            continue
        out.append(
            Event(
                source_id=source.id,
                title=title,
                start_at=None,
                venue=None,
                url=url,
            )
        )
    return out


def parse_nfm_repertuar(source: Source, html: str) -> list[Event]:
    # NFM repertuar is a listing with month/day + time (often without year in the visible card).
    # Extract (DD.MM[.YYYY] HH:MM) from the card DOM so window filtering works.
    s = soup(html)
    out: list[Event] = []
    seen: set[str] = set()
    now_y = datetime.now().year

    for a in s.select("a[href*='/component/nfmcalendar/event/']"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        url = urljoin(source.url, href)
        p = urlparse(url)
        if not (p.scheme and p.netloc and p.path):
            continue
        clean_url = f"{p.scheme}://{p.netloc}{p.path}"
        if clean_url in seen:
            continue

        # The event row container holds date/time blocks.
        card = a.find_parent(class_="nfmELItem")

        t = _clean(a.get_text(" ", strip=True) or "")
        if not t or t.lower() in _JUNK_TITLE_EQ or _JUNK_TITLE_RE.match(t):
            # Some buttons/tiles use "Szczegóły" etc.; ignore those.
            continue

        date_txt = ""
        time_txt = ""
        if card:
            de = card.select_one(".nfmEDDate")
            te = card.select_one(".nfmEDTime")
            date_txt = _clean(de.get_text(" ", strip=True) if de else "")
            time_txt = _clean(te.get_text(" ", strip=True) if te else "")

        dmy = None
        md = re.search(r"^(\\d{1,2})\\.(\\d{1,2})(?:\\.(\\d{4}))?$", date_txt)
        if md:
            dom, mon = int(md.group(1)), int(md.group(2))
            if 1 <= mon <= 12 and 1 <= dom <= 31:
                yr = int(md.group(3)) if md.group(3) else now_y
                dmy = f"{dom:02d}.{mon:02d}.{yr}"
        tm = None
        mt = re.search(r"^(\\d{1,2}:\\d{2})$", time_txt)
        if mt:
            tm = mt.group(1)
        raw_when = None
        if dmy and tm:
            raw_when = f"{dmy} {tm}"
        elif dmy:
            raw_when = dmy
        else:
            # Without a date, NFM listings create undated rows that bypass the window filter.
            continue

        seen.add(clean_url)
        out.append(Event(source_id=source.id, title=t, start_at=None, venue="NFM", url=clean_url, raw_date_text=raw_when))
        if len(out) >= 160:
            break
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
        "krajownik_wroclaw_wydarzenia": parse_krajownik_wroclaw_wydarzenia,
        "hala_stulecia": parse_hala_stulecia,
        "tarczynski_arena": parse_tarczynski_arena,
        "nfm_repertuar": parse_nfm_repertuar,
        "meetup_find": parse_meetup_find,
        "inyourpocket_events": parse_inyourpocket_events,
        "wroclawguide_calendar": parse_wroclawguide_calendar,
        "grotowski_wydarzenia": parse_grotowski_wydarzenia,
        "kino_nh": parse_kino_nh,
        # place-holders:
        "wydarzenia_wroclaw": parse_generic_links,
        "pik": parse_pik,
        "crossweb": parse_generic_links,
        "ebilet_city": parse_ebilet_city,
        "ebilet_pl": parse_ebilet_pl,
        "nowiny_olesnickie_wydarzenia": parse_nowiny_olesnickie_wydarzenia,
        "wroclaw_travel_calendar": parse_generic_links,
    }.get(kind, parse_generic_links)
