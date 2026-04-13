from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, time as dt_time, timezone, tzinfo
from urllib.parse import unquote, urlparse

from rapidfuzz.fuzz import partial_ratio, token_set_ratio

from .models import Event


_SPACE_RE = re.compile(r"\s+")
_FB_EVENT_ID_IN_URL = re.compile(r"facebook\.com/events/(\d+)", re.I)
_MEETUP_EVENT_ID_IN_URL = re.compile(r"meetup\.com/.*/events/(\d+)", re.I)
_GO_PATH_EVENT_ID = re.compile(r"/go/wydarzenia/[^/]+/(\d+)-", re.I)
_GO_PATH_TAIL = re.compile(r"/go/wydarzenia/([^/]+)/(\d+)-([^/]+)$", re.I)
_RAW_DMY_FULL = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})\b")
# Krajownik card titles: "12 Kwietnia 2026 , od 16:00 …"
_PL_DMY_IN_TITLE = re.compile(
    r"\b(\d{1,2})\s+([a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ]+)\s+(\d{4})\b",
    re.UNICODE,
)


def _fold_month_token(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").casefold())
    return "".join(c for c in s if not unicodedata.combining(c))


_PL_MONTH_BY_NAME: dict[str, int] = {
    "stycznia": 1,
    "lutego": 2,
    "marca": 3,
    "kwietnia": 4,
    "maja": 5,
    "czerwca": 6,
    "lipca": 7,
    "sierpnia": 8,
    "wrzesnia": 9,
    "wrzesnia": 9,
    "pazdziernika": 10,
    "listopada": 11,
    "grudnia": 12,
}


def _pl_date_from_krajownik_title(title: str) -> date | None:
    m = _PL_DMY_IN_TITLE.search(title or "")
    if not m:
        return None
    dom, monw, y = int(m.group(1)), _fold_month_token(m.group(2)), int(m.group(3))
    mon = _PL_MONTH_BY_NAME.get(monw)
    if not mon:
        return None
    try:
        return date(y, mon, dom)
    except ValueError:
        return None


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = _SPACE_RE.sub(" ", s)
    return s


def _is_krajownik_wroclaw_event_detail(url: str) -> bool:
    p = urlparse(url or "")
    net = (p.netloc or "").lower()
    if "krajownik.pl" not in net:
        return False
    parts = [x for x in (p.path or "").split("/") if x]
    if len(parts) < 3:
        return False
    return parts[0].lower() == "wroclaw" and parts[1].lower() == "wydarzenia"


# WordPress duplicate permalinks: same title republished as …-2, …-3. Keep 4+ digit tails (years, ids).
_WW_PATH_LEAF_DUP_SUFFIX = re.compile(r"-\d{1,3}$", re.I)


def _wydarzenia_wroclaw_path_key(url: str) -> str | None:
    """Stable key for wydarzenia.wroclaw.pl event pages (was title|…, caused repeats vs Krajownik)."""
    p = urlparse(url or "")
    net = (p.netloc or "").lower()
    if "wydarzenia.wroclaw.pl" not in net:
        return None
    path = unquote((p.path or "").strip().rstrip("/")).lower()
    if len(path) < 3:
        return None
    parts = [x for x in path.split("/") if x]
    if not parts:
        return None
    leaf = parts[-1]
    stem = _WW_PATH_LEAF_DUP_SUFFIX.sub("", leaf)
    if stem != leaf:
        parts[-1] = stem
        path = "/" + "/".join(parts)
    return path


def _krajownik_slug_stem(url: str) -> str | None:
    """Normalize krajownik detail slug so duplicate listings (different numeric ids) share a key."""
    if not _is_krajownik_wroclaw_event_detail(url):
        return None
    parts = [x for x in (urlparse(url).path or "").split("/") if x]
    leaf = unquote(parts[-1]).strip().lower()
    if not leaf or not re.search(r"-\d+$", leaf):
        return None
    stem = re.sub(r"-\d+$", "", leaf)
    stem = re.sub(r"^wroclaw-", "", stem, flags=re.I)
    return stem or None


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


def _dedupe_tz() -> tzinfo:
    import os

    from dateutil import tz as dttz

    z = dttz.gettz(os.environ.get("TIMEZONE", "Europe/Warsaw"))
    return z if z is not None else timezone.utc


def _event_local_date(ev: Event, local_tz: tzinfo) -> date | None:
    st = _aware_local_start(ev, local_tz)
    if st:
        return st.date()
    d = _parse_pl_date_from_raw(ev.raw_date_text)
    if d:
        return d
    return _pl_date_from_krajownik_title(ev.title or "")


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


def is_same_show_cross_source(a: Event, b: Event) -> bool:
    """Match krajownik long cards to short wydarzenia.wroclaw.pl / go.wroclaw titles, same calendar day."""
    ta, tb = _norm(a.title), _norm(b.title)
    if not ta or not tb:
        return False
    lo, sh = (ta, tb) if len(ta) >= len(tb) else (tb, ta)
    if len(sh) < 8 or (len(lo) < 12 and len(sh) < 12):
        return False
    tsr = token_set_ratio(ta, tb)
    par_ls = partial_ratio(lo, sh)
    par_sl = partial_ratio(sh, lo)
    best_par = max(par_ls, par_sl)
    # Allow shared rare tokens ("1991", show name) where token_set_ratio stays ~mid.
    if tsr < 44 and best_par < 62:
        return False
    if tsr < 38 and best_par < 68:
        return False
    va, vb = _norm(a.venue or ""), _norm(b.venue or "")
    if va and vb:
        if token_set_ratio(va, vb) < 58:
            return False
    else:
        sole = va or vb
        if sole:
            if not (
                sole in lo
                or sole in sh
                or token_set_ratio(sole, lo) >= 50
                or token_set_ratio(sole, sh) >= 50
            ):
                return False
        else:
            # No venue on either side (common for generic scrapers): need stronger title match.
            if tsr < 44 and best_par < 68:
                return False
    return True


def should_skip_cross_source_duplicate(ev: Event, seen: list[Event], local_tz: tzinfo) -> bool:
    """Skip when an event already ingested this run matches the same show (other site / URL)."""
    d_ev = _event_local_date(ev, local_tz)
    for r in seen:
        d_r = _event_local_date(r, local_tz)
        if d_ev is not None and d_r is not None and d_ev != d_r:
            continue
        if d_ev is None and d_r is None:
            continue
        if not (is_same_show_cross_source(ev, r) or is_same_show_cross_source(r, ev)):
            continue
        # wydarzenia.wroclaw.pl often has no date in the anchor; Krajownik has "12 Kwietnia …".
        if (d_ev is None) ^ (d_r is None):
            ta, tb = _norm(ev.title), _norm(r.title)
            lo, sh = (ta, tb) if len(ta) >= len(tb) else (tb, ta)
            pr = max(partial_ratio(lo, sh), partial_ratio(sh, lo))
            if pr < 48:
                continue
            if pr < 62 and token_set_ratio(ta, tb) < 42:
                continue
        return True
    return False


def fingerprint(ev: Event) -> str:
    # One row per Facebook event permalink; anchor text on search pages is unstable.
    mfb = _FB_EVENT_ID_IN_URL.search(ev.url or "")
    if mfb:
        return f"fb:{mfb.group(1)}"
    # Same for Meetup: titles on listing pages can include volatile counts/badges.
    mmu = _MEETUP_EVENT_ID_IN_URL.search(ev.url or "")
    if mmu:
        return f"meetup:{mmu.group(1)}"
    stem = _krajownik_slug_stem(ev.url or "")
    if stem:
        # Stem only: date suffix caused undated vs parsed flips and hourly duplicate posts.
        return f"kraj:{stem}"
    wwk = _wydarzenia_wroclaw_path_key(ev.url or "")
    if wwk:
        return f"ww:{wwk}"
    goid = _wroclaw_go_numeric_id(ev.url or "")
    if goid:
        # Numeric id only: date-based keys drift between runs (LD vs anchor, startDate tweaks),
        # which caused hourly Telegram repeats. Rows are removed after start_at passes (prune).
        return f"go:{goid}"
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

