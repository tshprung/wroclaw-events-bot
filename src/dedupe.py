from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, time as dt_time, timezone, tzinfo
from urllib.parse import unquote, urlparse

from dateutil import tz as dttz

from rapidfuzz.fuzz import token_set_ratio

from .models import Event


_SPACE_RE = re.compile(r"\s+")
_FB_EVENT_ID_IN_URL = re.compile(r"facebook\.com/events/(\d+)", re.I)
_MEETUP_EVENT_ID_IN_URL = re.compile(r"meetup\.com/.*/events/(\d+)", re.I)
_GO_PATH_EVENT_ID = re.compile(r"/go/wydarzenia/[^/]+/(\d+)-", re.I)
_GO_PATH_TAIL = re.compile(r"/go/wydarzenia/([^/]+)/(\d+)-([^/]+)$", re.I)
_RAW_DMY_FULL = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})\b")


def _fingerprint_local_tz() -> tzinfo:
    name = os.environ.get("TIMEZONE", "Europe/Warsaw")
    z = dttz.gettz(name)
    return z if z is not None else timezone.utc


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = _SPACE_RE.sub(" ", s)
    return s


def _wroclaw_go_numeric_id(url: str) -> str | None:
    """Numeric wroclaw.pl/go event id from URL path (works for relative hrefs too)."""
    p = urlparse(url or "")
    net = (p.netloc or "").lower()
    if net and "wroclaw.pl" not in net:
        return None
    path = unquote(p.path or "")
    m = _GO_PATH_EVENT_ID.search(path)
    return m.group(1) if m else None


def _parse_pl_date_from_raw(raw: str | None) -> date | None:
    """First DD.MM[.RRRR] in anchor-style raw text (same family as wroclaw.pl/go cards)."""
    if not raw or not raw.strip():
        return None
    m = _RAW_DMY_FULL.search(raw.strip())
    if not m:
        return None
    dom, mon, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    try:
        return date(y, mon, dom)
    except ValueError:
        return None


def _go_fingerprint_time_key(ev: Event) -> str:
    """Local calendar date for start_at, else date parsed from raw_date_text, else undated.

    JSON-LD rows have start_at; anchor-only rows often have raw_date_text only. Without this,
    those become go:<id>:undated vs go:<id>:2026-04-11 and duplicate Telegram posts.
    """
    if ev.start_at:
        t = ev.start_at
        loc = _fingerprint_local_tz()
        if t.tzinfo is None:
            t = t.replace(tzinfo=loc)
        else:
            t = t.astimezone(loc)
        return t.date().isoformat()
    d = _parse_pl_date_from_raw(ev.raw_date_text)
    if d:
        return d.isoformat()
    return "undated"


def _normalized_go_detail_key(url: str) -> str | None:
    """Stable key for one wroclaw.pl/go detail permalink (scheme-insensitive)."""
    if not _wroclaw_go_numeric_id(url or ""):
        return None
    p = urlparse(url or "")
    net = (p.netloc or "").lower()
    if net.startswith("www."):
        net = net[4:]
    path = unquote((p.path or "").rstrip("/")).lower()
    return f"{net}{path}"


def _aware_local_start(ev: Event, local_tz: tzinfo) -> datetime | None:
    if not ev.start_at:
        return None
    t = ev.start_at
    if t.tzinfo is None:
        t = t.replace(tzinfo=local_tz)
    else:
        t = t.astimezone(local_tz)
    return t


def _merge_sort_key(ev: Event, local_tz: tzinfo) -> tuple[date, int, datetime]:
    """Earlier calendar day wins; same day prefers rows with real start_at over raw-only."""
    st = _aware_local_start(ev, local_tz)
    if st:
        return (st.date(), 0, st)
    d = _parse_pl_date_from_raw(ev.raw_date_text)
    if d:
        noon = datetime.combine(d, dt_time(12, 0), tzinfo=local_tz)
        return (d, 1, noon)
    return (date.max, 2, datetime.max.replace(tzinfo=local_tz))


def _merge_go_duplicate_group(members: list[Event], local_tz: tzinfo) -> Event:
    best = min(members, key=lambda e: _merge_sort_key(e, local_tz))
    for e in members:
        if e is best:
            continue
        kw: dict = {}
        if best.raw_date_text is None and e.raw_date_text:
            kw["raw_date_text"] = e.raw_date_text
        if best.venue is None and e.venue:
            kw["venue"] = e.venue
        if best.start_at is None and e.start_at is not None:
            kw["start_at"] = e.start_at
        if kw:
            best = replace(best, **kw)
    return best


def collapse_wroclaw_go_same_detail_url(events: list[Event], local_tz: tzinfo) -> list[Event]:
    """One row per go.wroclaw detail URL per batch (multiple JSON-LD Event blocks, LD + anchor, etc.)."""
    groups: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        k = _normalized_go_detail_key(e.url)
        if k is None:
            continue
        groups[k].append(e)
    merged: dict[str, Event] = {k: _merge_go_duplicate_group(v, local_tz) for k, v in groups.items() if len(v) > 1}
    if not merged:
        return events
    emitted: set[str] = set()
    out: list[Event] = []
    for e in events:
        k = _normalized_go_detail_key(e.url)
        if k is None:
            out.append(e)
            continue
        if k in emitted:
            continue
        emitted.add(k)
        out.append(merged.get(k, e))
    return out


def _wroclaw_go_path_tail(url: str) -> tuple[str | None, str | None, str | None]:
    """(category, numeric_id, slug_after_id) or (None, None, None)."""
    p = urlparse(url or "")
    net = (p.netloc or "").lower()
    if net and "wroclaw.pl" not in net:
        return None, None, None
    path = unquote(p.path or "")
    m = _GO_PATH_TAIL.search(path)
    if not m:
        return None, None, None
    return m.group(1).lower(), m.group(2), m.group(3).lower()


def collapse_wroclaw_go_twin_listings(events: list[Event], local_tz: tzinfo) -> list[Event]:
    """When the city site lists one performance under two numeric ids, keep a single row.

    Groups by (category, first slug token, local calendar date). Prefer the URL whose
    slug segment is longest (usually the fuller detail page).
    """
    groups: dict[tuple[str, str, date], list[tuple[int, Event]]] = defaultdict(list)
    for i, e in enumerate(events):
        cat, _eid, slug = _wroclaw_go_path_tail(e.url)
        if not cat or not slug or not e.start_at:
            continue
        slug0 = slug.split("-")[0]
        if len(slug0) < 4:
            continue
        d = e.start_at.astimezone(local_tz).date()
        groups[(cat, slug0, d)].append((i, e))

    drop_idx: set[int] = set()
    for members in groups.values():
        if len(members) <= 1:
            continue

        def score(it: tuple[int, Event]) -> tuple[int, int]:
            _idx, ev = it
            _c, _i, sl = _wroclaw_go_path_tail(ev.url)
            return (len(sl or ""), len(ev.url or ""))

        best_idx, _best_ev = max(members, key=score)
        for idx, _ev in members:
            if idx != best_idx:
                drop_idx.add(idx)

    if not drop_idx:
        return events
    return [e for i, e in enumerate(events) if i not in drop_idx]


def fingerprint(ev: Event) -> str:
    # One row per Facebook event permalink; anchor text on search pages is unstable.
    mfb = _FB_EVENT_ID_IN_URL.search(ev.url or "")
    if mfb:
        return f"fb:{mfb.group(1)}"
    # Same for Meetup: titles on listing pages can include volatile counts/badges.
    mmu = _MEETUP_EVENT_ID_IN_URL.search(ev.url or "")
    if mmu:
        return f"meetup:{mmu.group(1)}"
    goid = _wroclaw_go_numeric_id(ev.url or "")
    if goid:
        # Local date: same id + day is one row; recurring dates get new keys after prune.
        return f"go:{goid}:{_go_fingerprint_time_key(ev)}"
    title = _norm(ev.title)
    venue = _norm(ev.venue or "")
    start = ev.start_at.isoformat() if ev.start_at else ""
    return f"{title}|{start}|{venue}"


def is_probably_same(a: Event, b: Event) -> bool:
    if a.start_at and b.start_at:
        if abs((a.start_at - b.start_at).total_seconds()) > 2 * 3600:
            return False
    if (a.venue or "") and (b.venue or ""):
        if token_set_ratio(_norm(a.venue or ""), _norm(b.venue or "")) < 80:
            return False
    return token_set_ratio(_norm(a.title), _norm(b.title)) >= 88

