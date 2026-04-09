from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .models import Event

# Used only for scheduling: Event.start_at and Event.raw_date_text from the source page.
# Never uses DB ingestion timestamps (e.g. first_seen_at).


def _fold_pl(s: str) -> str:
    """Lowercase and strip Polish diacritics for robust keyword matching."""
    s = unicodedata.normalize("NFKD", s.casefold())
    return "".join(c for c in s if not unicodedata.combining(c))


_WD_NAME_TO_ISO = {
    "poniedzialek": 0,
    "wtorek": 1,
    "sroda": 2,
    "czwartek": 3,
    "piatek": 4,
    "sobota": 5,
    "niedziela": 6,
}


def _next_calendar_weekday(from_day: date, target_weekday: int) -> date:
    """Next occurrence of weekday (Mon=0) on or after from_day."""
    delta = (target_weekday - from_day.weekday() + 7) % 7
    return from_day + timedelta(days=delta)


_TIME_O_CLOCK = re.compile(r"\bo\s*(\d{1,2})\s*[:.](\d{2})\b", re.IGNORECASE)
# Colon only — DD.MM calendar dates must not match as a "time".
_TIME_COMPACT = re.compile(r"\b(\d{1,2}):(\d{2})\b")
# Polish day-month numeric: 12.04, 08.05.2026 (before clock time in wroclaw.pl/go strings).
_DOT_DM = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b")

_REL_DZIS = re.compile(r"\b(dzis|dzisiaj)\b", re.IGNORECASE)
_REL_JUTRO = re.compile(r"\bjutro\b", re.IGNORECASE)
_REL_POJUTRZE = re.compile(r"\bpojutrze\b", re.IGNORECASE)
_WEEKDAY_WORD = re.compile(
    r"\b(poniedziałek|poniedzialek|wtorek|środa|sroda|czwartek|piątek|piatek|sobota|niedziela)\b",
    re.IGNORECASE,
)
_DAY_MONTH = re.compile(r"\b(\d{1,2})\s+([a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ]+)\b")

_MONTH_NAMES: dict[str, int] = {}
for _num, _labels in (
    (1, ("sty", "stycznia", "styczen")),
    (2, ("lut", "lutego", "luty")),
    (3, ("mar", "marca", "marzec")),
    (4, ("kwi", "kwietnia", "kwiecien", "kwieten")),
    (5, ("maj", "maja")),
    (6, ("cze", "czerwca", "czerwiec")),
    (7, ("lip", "lipca", "lipiec")),
    (8, ("sie", "sierpnia", "sierpien")),
    (9, ("wrz", "wrzesnia", "wrzesien", "wrzesn")),
    (10, ("paz", "paź", "października", "pazdziernika")),
    (11, ("lis", "listopada", "listopad")),
    (12, ("gru", "grudnia", "grudzien")),
):
    for _lab in _labels:
        _MONTH_NAMES[_fold_pl(_lab)] = _num


def _month_num(token: str) -> int | None:
    f = _fold_pl(token.strip())
    if f in _MONTH_NAMES:
        return _MONTH_NAMES[f]
    for key, num in _MONTH_NAMES.items():
        if f.startswith(key) or key.startswith(f):
            return num
    return None


def _extract_time_portion(text: str) -> time | None:
    m = _TIME_O_CLOCK.search(text)
    if not m:
        m = _TIME_COMPACT.search(text)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return time(h, mi)
    return None


def _parsed_dot_date(prefix: str, now: datetime, tm: time | None) -> _Resolved | None:
    """Take DD.MM[.RRRR] from the part of the string before `o HH:MM` (PL style)."""
    chosen: date | None = None
    y_now = now.date().year
    for m in _DOT_DM.finditer(prefix):
        dom, mon = int(m.group(1)), int(m.group(2))
        if not (1 <= mon <= 12 and 1 <= dom <= 31):
            continue
        yraw = m.group(3)
        if yraw:
            y = int(yraw)
            if y < 100:
                y += 2000
        else:
            y = y_now
        try:
            cand = date(y, mon, dom)
        except ValueError:
            continue
        if not yraw and cand < now.date():
            try:
                cand = date(y + 1, mon, dom)
            except ValueError:
                continue
        chosen = cand
    if chosen is None:
        return None
    return _Resolved(chosen, tm)


@dataclass(frozen=True)
class _Resolved:
    day: date
    tm: time | None  # None => whole calendar day


def _coerce_start_at(ev: Event, now: datetime) -> _Resolved | None:
    if ev.start_at is None:
        return None
    s = ev.start_at
    if s.tzinfo is None:
        s = s.replace(tzinfo=now.tzinfo)
    else:
        s = s.astimezone(now.tzinfo)
    return _Resolved(s.date(), s.time())


def _parse_raw_when(raw: str, now: datetime) -> _Resolved | None:
    t = (raw or "").strip()
    if not t:
        return None
    folded_line = _fold_pl(t)
    tm = _extract_time_portion(t)
    d = now.date()

    clock_m = _TIME_O_CLOCK.search(t)
    prefix = t[: clock_m.start()] if clock_m else t

    dotted = _parsed_dot_date(prefix, now, tm)
    if dotted is not None:
        return dotted

    if _REL_DZIS.search(folded_line):
        return _Resolved(d, tm)
    if _REL_JUTRO.search(folded_line):
        return _Resolved(d + timedelta(days=1), tm)
    if _REL_POJUTRZE.search(folded_line):
        return _Resolved(d + timedelta(days=2), tm)

    wm = _WEEKDAY_WORD.search(t)
    if wm:
        name = _fold_pl(wm.group(1))
        target = _WD_NAME_TO_ISO.get(name)
        if target is None:
            return None
        day = _next_calendar_weekday(d, target)
        return _Resolved(day, tm)

    dm = _DAY_MONTH.search(t)
    if dm:
        dom = int(dm.group(1))
        mon = _month_num(dm.group(2))
        if mon is None or not 1 <= dom <= 31:
            return None
        year = d.year
        try:
            resolved = date(year, mon, dom)
        except ValueError:
            return None
        if resolved < d - timedelta(days=400):
            return None
        if resolved < d:
            try:
                resolved = date(year + 1, mon, dom)
            except ValueError:
                return None
        return _Resolved(resolved, tm)

    return None


def resolve_when(ev: Event, now: datetime) -> _Resolved | None:
    """Resolve when the event occurs (start), never when it was scraped or stored."""
    r = _coerce_start_at(ev, now)
    if r is not None:
        return r
    if ev.raw_date_text:
        return _parse_raw_when(ev.raw_date_text, now)
    return None


def _resolved_in_window(r: _Resolved, now_local: datetime, window_end: datetime) -> bool:
    """True if the event overlaps [now_local, window_end]."""
    tz = now_local.tzinfo
    if r.tm is not None:
        ev_dt = datetime.combine(r.day, r.tm, tzinfo=tz)
        return now_local <= ev_dt <= window_end
    start_day = datetime.combine(r.day, time.min, tzinfo=tz)
    end_day = datetime.combine(r.day, time.max.replace(microsecond=999999), tzinfo=tz)
    return not (end_day < now_local or start_day > window_end)


def event_in_window(
    ev: Event,
    now_local: datetime,
    *,
    window_end: datetime,
    include_undated: bool,
) -> bool:
    r = resolve_when(ev, now_local)
    if r is None:
        return include_undated
    return _resolved_in_window(r, now_local, window_end)


def load_window_options() -> tuple[int, bool]:
    days = int(os.environ.get("EVENT_WINDOW_DAYS", "7"))
    if days < 1:
        days = 7
    # Default 1: many Phase-1 parsers do not emit dates yet; set EVENT_WINDOW_INCLUDE_UNDATED=0 for strict mode.
    include_undated = os.environ.get("EVENT_WINDOW_INCLUDE_UNDATED", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    return days, include_undated


def filter_events_in_window(events: list[Event], now_local: datetime) -> list[Event]:
    days, include_undated = load_window_options()
    window_end = now_local + timedelta(days=days)
    return [
        e
        for e in events
        if event_in_window(e, now_local, window_end=window_end, include_undated=include_undated)
    ]
