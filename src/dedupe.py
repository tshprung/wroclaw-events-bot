from __future__ import annotations

import re
from datetime import datetime

from rapidfuzz.fuzz import token_set_ratio

from .models import Event


_SPACE_RE = re.compile(r"\s+")
_FB_EVENT_ID_IN_URL = re.compile(r"facebook\.com/events/(\d+)", re.I)


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = _SPACE_RE.sub(" ", s)
    return s


def fingerprint(ev: Event) -> str:
    # One row per Facebook event permalink; anchor text on search pages is unstable.
    mfb = _FB_EVENT_ID_IN_URL.search(ev.url or "")
    if mfb:
        return f"fb:{mfb.group(1)}"
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

