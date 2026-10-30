"""Recover useful event identity from listing cards, including older stored rows."""
from __future__ import annotations

import re
import os
from dataclasses import replace
from datetime import datetime

from dateutil import tz

from .models import Event
from .family import fold


_CARD_DATE = re.compile(r"\b(\d{1,2})\s+([a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ]+)\s+(\d{4})\b")
_CARD_TIME = re.compile(r"\b(?:od\s*)?(\d{1,2}):(\d{2})\b")
_CARD_VENUE = re.compile(r"\b(?:od\s*)?\d{1,2}:\d{2}\s+Wroc[łl]aw\s+(.+)$", re.I)
_PLACEHOLDER = re.compile(r"^Wydarzenie\s*\([^)]*\)$", re.I)
_MONTHS = {name: i for i, name in enumerate(("stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca",
    "lipca", "sierpnia", "wrzesnia", "pazdziernika", "listopada", "grudnia"), 1)}


def event_title(ev: Event) -> str:
    title = ev.title.strip()
    if _PLACEHOLDER.match(title) and ev.raw_date_text:
        date_match = _CARD_DATE.search(ev.raw_date_text)
        before = ev.raw_date_text[:date_match.start()].strip(" ,·—–-") if date_match else ""
        if before:
            return before
    # Some sources put the entire card in the title.
    date_match = _CARD_DATE.search(title)
    return title[:date_match.start()].strip(" ,·—–-") if date_match and date_match.start() else title


def normalize_event(ev: Event) -> Event:
    """Repair display and matching fields without changing the stored fingerprint."""
    title = event_title(ev)
    venue = ev.venue
    start_at = ev.start_at
    if not venue and ev.raw_date_text and "krajownik.pl" in ev.url:
        match = _CARD_VENUE.search(ev.raw_date_text)
        if match:
            venue = match[1].strip()
    if start_at is None and ev.raw_date_text and "krajownik.pl" in ev.url:
        day = _CARD_DATE.search(ev.raw_date_text)
        clock = _CARD_TIME.search(ev.raw_date_text[day.end():]) if day else None
        month = _MONTHS.get(fold(day[2])) if day else None
        if day and clock and month:
            try:
                start_at = datetime(int(day[3]), month, int(day[1]), int(clock[1]), int(clock[2]),
                                    tzinfo=tz.gettz(os.environ.get("TIMEZONE", "Europe/Warsaw")))
            except ValueError:
                pass
    return replace(ev, title=title, venue=venue, start_at=start_at)
