"""Drop events the operator does not want (URLs, venues, wroclaw.pl/go categories)."""

from __future__ import annotations

import os
import re
import unicodedata
from urllib.parse import urlparse

from .models import Event

_SPACE = re.compile(r"\s+")


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return _SPACE.sub(" ", s).strip()


# wroclaw.pl/go category segment — e.g. /go/wydarzenia/sztuka/123-slug
_DEFAULT_URL_PARTS: frozenset[str] = frozenset(
    {
        "/go/wydarzenia/sztuka/",
        "muzeumpanatadeusza",
        "mnwr.pl",
        "panoramaraclawicka.pl",
        "muzeumarchitektury.pl",
        "muzeum.miejskie.wroclaw",
        "bwa.wroc.pl",
        # Static info pages, not events.
        "zoo.wroclaw.pl/zwiedzanie/godziny-otwarcia",
        # Senior-care site at staryklasztor.pl (not Wrocław club); drop if linked elsewhere.
        "staryklasztor.pl",
        # WTeatrW CMS: /Repertuar,N is the repertoire index, not a single show.
        "wteatrw.pl/repertuar,",
        # WTeatrW: combined ticket bundles and in-page ticket anchors, not one show date.
        "bilet_laczony",
        "#to_tickets",
        # GoOut marketing / shop list, not events (other pages may still link here).
        "goout.net/pl/ticket-shops",
        # Kino Nowe Horyzonty ticketing / showtimes (not "events" for this bot).
        "kinonh.pl/bilet.s",
        # WTL: permanent show pages under /pl/spektakle/…, not dated performances.
        "teatrlalek.wroclaw.pl/pl/spektakle/",
        # eBilet category hub (list of concerts), not one event.
        "ebilet.pl/klasyka/koncert",
        # wydarzenia.wroclaw.pl editorial / topic hubs (not single events).
        "wydarzenia.wroclaw.pl/blog/",
        # wroclaw.pl/go listing anchors — same calendar row, not a permalink.
        "#evt-",
        # osiedle.wroc.pl: admin/news item (not an event).
        "zmiany-w-satucie",
        # wroclaw.pl: informational/category hubs (not single events).
        "wroclaw.pl/poradnik-mieszkanca/kategoria/",
        "wroclaw.pl/inwestycje-wroclaw/aktualnosci-inwestycje",
        # wroclaw.pl: investment / culture / sport index pages (not single events).
        "wroclaw.pl/inwestycje-wroclaw/inwestycje-",
        "wroclaw.pl/kultura/aktualnosci",
        "wroclaw.pl/kultura/stypendia-artystyczne",
        "wroclaw.pl/kultura/wroclawskie-festiwale",
        "wroclaw.pl/kultura/miejskie-instytucje-kultury",
        "wroclaw.pl/kultura/wroclawska-rada-kultury",
        "wroclaw.pl/sport/aktualnosci-sport-i-rekreacja-wroclaw",
        "wroclaw.pl/sport/rekreacja-we-wroclawiu",
        # wroclaw.pl/go and wydarzenia.wroclaw.pl topic hubs (not single events).
        "wroclaw.pl/go/wydarzenia/teatr",
        "wroclaw.pl/go/wydarzenia/muzyka",
        "wydarzenia.wroclaw.pl/muzyka_pop",
        "wydarzenia.wroclaw.pl/muzyka_jazz",
        # Teatr Capitol: ticket-sale blog posts and memorial / news pages (not dated shows).
        "teatr-capitol.pl/rozpoczynamy-sprzedaz-biletow",
        "teatr-capitol.pl/pozegnanie-",
    }
)

_DEFAULT_VENUE_PARTS: frozenset[str] = frozenset(
    {
        "muzeum pana tadeusza",
        "muzeum narodowe",
        "panorama raclawicka",
        "muzeum architektury",
        "bwa wroclaw",
    }
)

_DEFAULT_TITLE_PARTS: frozenset[str] = frozenset(
    {
        # Ticket-sales announcements (not a concrete, dated event page).
        "sprzedaz biletow",
        "rozpoczynamy sprzedaz biletow",
        # Polish “there are tickets on sale…” announcements (same idea as sprzedaż).
        "sa bilety na",
        # Osiedle council admin/news posts (not events).
        "statut",
        "statucie",
    }
)

# osiedle.wroc.pl only — Rada Osiedla / komisje (formal meetings, not public “events”).
_OSIEDLE_NON_EVENT_TITLE_PARTS: frozenset[str] = frozenset(
    {
        "sesja rady osiedla",
        "sesja rady dzielnicy",
        "zebranie rady osiedla",
        "zebranie komisji",
        "posiedzenie komisji",
        "posiedzenie rady osiedla",
        "nadzwyczajna sesja rady",
        "komisja rewizyjna",
        "komisji rewizyjnej",
        "spotkanie komisji",
        # Urban-planning consultations / map info (not neighbourhood “events”).
        "konsultacje planu ogolnego wroclawia",
        "konsultacje planu",
        "plan miejscowy",
        "na planie ogolnym",
        "plan ogolnego wroclawia",
        "twoja okolica na planie",
    }
)

# krajownik.pl only — open calls / recruitment pages listed among events.
_KRAJOWNIK_NON_EVENT_TITLE_PARTS: frozenset[str] = frozenset(
    {
        "otwarty nabor",
        "otwarty nabor do projektu",
        "nabor do projektu",
        "naboru do projektu",
        "rekrutacja do projektu",
    }
)


def _extra_from_env(var: str) -> frozenset[str]:
    raw = os.environ.get(var, "").strip()
    if not raw:
        return frozenset()
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())


def _all_url_parts() -> frozenset[str]:
    return _DEFAULT_URL_PARTS | _extra_from_env("EVENT_EXCLUDE_URL_SUBSTR")


def _all_venue_parts() -> frozenset[str]:
    return _DEFAULT_VENUE_PARTS | _extra_from_env("EVENT_EXCLUDE_VENUE_SUBSTR")

def _all_title_parts() -> frozenset[str]:
    return _DEFAULT_TITLE_PARTS | _extra_from_env("EVENT_EXCLUDE_TITLE_SUBSTR")


def _all_osiedle_title_parts() -> frozenset[str]:
    return _all_title_parts() | _OSIEDLE_NON_EVENT_TITLE_PARTS | _extra_from_env(
        "EVENT_EXCLUDE_OSIEDLE_TITLE_SUBSTR"
    )


def _all_krajownik_title_parts() -> frozenset[str]:
    return _KRAJOWNIK_NON_EVENT_TITLE_PARTS | _extra_from_env("EVENT_EXCLUDE_KRAJOWNIK_TITLE_SUBSTR")


def _wroclaw_go_wydarzenia_category_landing(url: str) -> bool:
    """True for /go/wydarzenia/<category> with no /{id}-slug (e.g. …/kino vs …/kino/123-show)."""
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return False
    if "wroclaw.pl" not in (p.netloc or "").lower():
        return False
    parts = [x for x in (p.path or "").split("/") if x]
    if len(parts) != 3:
        return False
    return parts[0].lower() == "go" and parts[1].lower() == "wydarzenia"


def _wroclawguide_listing_hub_url(url: str) -> bool:
    """WroclawGuide calendar root, month views, and /events-category/* filters — not single events."""
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return False
    if "wroclawguide.com" not in (p.netloc or "").lower():
        return False
    path = (p.path or "").lower()
    if "/events-category/" in path:
        return True
    if "event-calendar-wroclaw" in path:
        return True
    return False


def event_is_excluded(ev: Event) -> bool:
    u_raw = (ev.url or "").strip()
    if not u_raw.lower().startswith("https://"):
        return True
    u = u_raw.lower()
    if _wroclawguide_listing_hub_url(u_raw):
        return True
    if _wroclaw_go_wydarzenia_category_landing(u_raw):
        return True
    for frag in _all_url_parts():
        if frag in u:
            return True
    # Operator policy: drop by URL keyword (case-insensitive via `u`).
    for kw in ("koncert", "teatr", "jazz", "dating"):
        if kw in u:
            return True

    # Title-based exclusions are only safe when scoped to known hosts that emit
    # lots of non-event news posts into the generic_links feed.
    host = ""
    try:
        host = (u.split("/")[2] if "://" in u else "").lower()
    except Exception:
        host = ""
    title_f = _fold(ev.title or "")
    if title_f:
        if "teatr-capitol.pl" in host:
            for frag in _all_title_parts():
                if frag in title_f:
                    return True
        if "osiedle.wroc.pl" in host:
            for frag in _all_osiedle_title_parts():
                if frag in title_f:
                    return True
        if "krajownik.pl" in host:
            for frag in _all_krajownik_title_parts():
                if frag in title_f:
                    return True
    ven = _fold(ev.venue or "")
    if not ven:
        return False
    for frag in _all_venue_parts():
        if frag in ven:
            return True
    return False


def filter_out_excluded_events(events: list[Event]) -> tuple[list[Event], int]:
    out = [e for e in events if not event_is_excluded(e)]
    return out, len(events) - len(out)
