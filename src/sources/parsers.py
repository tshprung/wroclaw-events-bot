from __future__ import annotations

import re
from datetime import datetime
from typing import Callable
from urllib.parse import urlparse

from dateutil import tz

from ..models import Event, Source
from .common import extract_links


_SPACE = re.compile(r"\s+")


def _clean(s: str) -> str:
    return _SPACE.sub(" ", (s or "").strip())


def parse_generic_links(source: Source, html: str) -> list[Event]:
    # Generic fallback: create low-fidelity “events” from prominent links.
    # This is meant as scaffolding; source-specific parsers should replace it.
    out: list[Event] = []
    links = extract_links(source.url, html, selector="a[href]", limit=50)
    seen = set()
    for text, url in links:
        key = (text, url)
        if key in seen:
            continue
        seen.add(key)
        out.append(Event(source_id=source.id, title=_clean(text), start_at=None, venue=None, url=url))
    return out


def parse_wroclaw_go(source: Source, html: str) -> list[Event]:
    # wroclaw.pl/go list pages include “Title Dziś o 18:00 Venue” in anchor text.
    links = extract_links(source.url, html, selector="a[href]", limit=120)
    out: list[Event] = []
    for text, url in links:
        if "/go/wydarzenia/" not in url:
            continue
        out.append(_parse_wroclaw_go_anchor(source.id, text, url))
    # Filter out empties
    return [e for e in out if e.title]


def _parse_wroclaw_go_anchor(source_id: str, text: str, url: str) -> Event:
    text = _clean(text)
    # Example: "Vertigo Swing Orchestra Old vs New Dziś o 19:00 Vertigo Jazz Club & Restaurant"
    m = re.search(r"^(?P<title>.+?)\s+(?P<when>(?:Dziś|Jutro|Pojutrze|Sobota|Niedziela|Poniedziałek|Wtorek|Środa|Czwartek|Piątek).+?)\s+(?P<venue>.+)$", text)
    title = text
    when_txt = None
    venue = None
    if m:
        title = _clean(m.group("title"))
        when_txt = _clean(m.group("when"))
        venue = _clean(m.group("venue"))
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


def parser_for_kind(kind: str) -> Callable[[Source, str], list[Event]]:
    return {
        "generic_links": parse_generic_links,
        "wroclaw_go": parse_wroclaw_go,
        "hala_stulecia": parse_hala_stulecia,
        "tarczynski_arena": parse_tarczynski_arena,
        "nfm_repertuar": parse_nfm_repertuar,
        "meetup_find": parse_meetup_find,
        "wroclawguide_calendar": parse_wroclawguide_calendar,
        # place-holders:
        "wydarzenia_wroclaw": parse_generic_links,
        "pik": parse_generic_links,
        "crossweb": parse_generic_links,
        "ebilet_city": parse_generic_links,
        "wroclaw_travel_calendar": parse_generic_links,
    }.get(kind, parse_generic_links)

