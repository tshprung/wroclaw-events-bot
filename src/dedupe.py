from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timezone, tzinfo
from urllib.parse import unquote, urlparse

from rapidfuzz.fuzz import token_set_ratio

from .models import Event


_SPACE_RE = re.compile(r"\s+")
_FB_EVENT_ID_IN_URL = re.compile(r"facebook\.com/events/(\d+)", re.I)
_MEETUP_EVENT_ID_IN_URL = re.compile(r"meetup\.com/.*/events/(\d+)", re.I)
_GO_PATH_EVENT_ID = re.compile(r"/go/wydarzenia/[^/]+/(\d+)-", re.I)
_GO_PATH_TAIL = re.compile(r"/go/wydarzenia/([^/]+)/(\d+)-([^/]+)$", re.I)


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


def _go_fingerprint_time_key(ev: Event) -> str:
    """UTC minute bucket so the same performance dedupes; distinct shows keep distinct rows."""
    if not ev.start_at:
        return "undated"
    t = ev.start_at
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    else:
        t = t.astimezone(timezone.utc)
    t = t.replace(second=0, microsecond=0)
    return t.isoformat()


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

        def slug_len(ev: Event) -> int:
            _c, _i, sl = _wroclaw_go_path_tail(ev.url)
            return len(sl or "")

        best = max((ev for _idx, ev in members), key=lambda ev: (slug_len(ev), len(ev.url or "")))
        best_key = (best.url, _go_fingerprint_time_key(best))
        for idx, ev in members:
            if (ev.url, _go_fingerprint_time_key(ev)) != best_key:
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
        # Include start time so recurring runs do not reuse one row (and re-alert after prune).
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

