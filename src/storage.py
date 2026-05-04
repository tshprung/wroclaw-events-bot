from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from .dedupe import _wydarzenia_wroclaw_path_key, fingerprint
from .models import Event


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS events (
  fingerprint TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  title TEXT NOT NULL,
  start_at TEXT,
  venue TEXT,
  url TEXT NOT NULL,
  city TEXT,
  raw_date_text TEXT,
  first_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_start_at_not_null
  ON events(start_at)
  WHERE start_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS bot_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_health (
  source_id TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  last_ok_at TEXT,
  last_fail_at TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_http_status INTEGER,
  last_error_kind TEXT,
  last_error_detail TEXT,
  last_sample_count INTEGER
);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  sources_total INTEGER,
  sources_ok INTEGER,
  sources_failed INTEGER,
  new_events INTEGER
);
"""

_META_GO_FP_COLLAPSE = "legacy_go_fingerprint_collapsed_v1"
_META_KRAJ_FP_COLLAPSE = "legacy_kraj_fingerprint_collapsed_v1"
_META_WW_FP_COLLAPSE = "legacy_ww_fingerprint_collapsed_v1"
_META_XSLUG_FP_COLLAPSE = "legacy_xslug_fingerprint_collapsed_v1"


def _event_from_storage_row(r: tuple) -> Event:
    _fp, source_id, title, start_at_s, venue, url, city, raw_date_text, _fst = r
    st: datetime | None = None
    if start_at_s:
        try:
            st = datetime.fromisoformat(str(start_at_s))
        except ValueError:
            st = None
        if st is not None and st.tzinfo is None:
            st = st.replace(tzinfo=timezone.utc)
    return Event(
        source_id=str(source_id),
        title=str(title),
        start_at=st,
        venue=venue,
        url=str(url),
        city=str(city or "Wrocław"),
        raw_date_text=raw_date_text,
    )


def _collapse_legacy_xslug_fingerprints(conn: sqlite3.Connection) -> None:
    """Merge title|start|venue rows into xslug:… when URL is WroclawGuide / Hala (same show, one DB row)."""
    if conn.execute(
        "SELECT 1 FROM bot_meta WHERE key=?",
        (_META_XSLUG_FP_COLLAPSE,),
    ).fetchone():
        return
    rows = conn.execute(
        """
        SELECT fingerprint, source_id, title, start_at, venue, url, city, raw_date_text, first_seen_at
        FROM events
        """
    ).fetchall()
    groups: dict[str, list[tuple]] = {}
    for r in rows:
        old_fp = r[0]
        ev = _event_from_storage_row(r)
        try:
            new_fp = fingerprint(ev)
        except Exception:
            continue
        if not new_fp.startswith("xslug:") or new_fp == old_fp:
            continue
        groups.setdefault(new_fp, []).append(r)
    for new_fp, members in groups.items():
        if len(members) == 1 and members[0][0] == new_fp:
            continue
        members.sort(key=lambda x: x[8])
        w = members[0]
        for r in members:
            conn.execute("DELETE FROM events WHERE fingerprint=?", (r[0],))
        conn.execute(
            """
            INSERT INTO events
              (fingerprint, source_id, title, start_at, venue, url, city, raw_date_text, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (new_fp, w[1], w[2], w[3], w[4], w[5], w[6], w[7], w[8]),
        )
    conn.execute(
        """
        INSERT INTO bot_meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (_META_XSLUG_FP_COLLAPSE, _now_utc_iso()),
    )


def _collapse_legacy_go_fingerprints(conn: sqlite3.Connection) -> None:
    """Merge go:<id>:… rows into go:<id> so deploy matches fingerprint(ev) and avoids double posts."""
    if conn.execute(
        "SELECT 1 FROM bot_meta WHERE key=?",
        (_META_GO_FP_COLLAPSE,),
    ).fetchone():
        return
    rows = conn.execute(
        """
        SELECT fingerprint, source_id, title, start_at, venue, url, city, raw_date_text, first_seen_at
        FROM events
        WHERE fingerprint GLOB 'go:*'
        """
    ).fetchall()
    groups: dict[str, list[tuple]] = {}
    for r in rows:
        fp = r[0]
        m = re.match(r"^go:(\d+)", fp)
        if not m:
            continue
        canon = f"go:{m.group(1)}"
        groups.setdefault(canon, []).append(r)
    for canon, members in groups.items():
        if len(members) == 1 and members[0][0] == canon:
            continue
        members.sort(key=lambda x: x[8])
        w = members[0]
        for r in members:
            conn.execute("DELETE FROM events WHERE fingerprint=?", (r[0],))
        conn.execute(
            """
            INSERT INTO events
              (fingerprint, source_id, title, start_at, venue, url, city, raw_date_text, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (canon, w[1], w[2], w[3], w[4], w[5], w[6], w[7], w[8]),
        )
    conn.execute(
        """
        INSERT INTO bot_meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (_META_GO_FP_COLLAPSE, _now_utc_iso()),
    )


def _collapse_legacy_kraj_fingerprints(conn: sqlite3.Connection) -> None:
    """Merge kraj:<stem>:… rows into kraj:<stem> (date suffix removed from fingerprint)."""
    if conn.execute(
        "SELECT 1 FROM bot_meta WHERE key=?",
        (_META_KRAJ_FP_COLLAPSE,),
    ).fetchone():
        return
    rows = conn.execute(
        """
        SELECT fingerprint, source_id, title, start_at, venue, url, city, raw_date_text, first_seen_at
        FROM events
        WHERE fingerprint GLOB 'kraj:*'
        """
    ).fetchall()
    groups: dict[str, list[tuple]] = {}
    for r in rows:
        fp = r[0]
        m = re.match(r"^kraj:([^:]+)", fp)
        if not m:
            continue
        canon = f"kraj:{m.group(1)}"
        groups.setdefault(canon, []).append(r)
    for canon, members in groups.items():
        if len(members) == 1 and members[0][0] == canon:
            continue
        members.sort(key=lambda x: x[8])
        w = members[0]
        for r in members:
            conn.execute("DELETE FROM events WHERE fingerprint=?", (r[0],))
        conn.execute(
            """
            INSERT INTO events
              (fingerprint, source_id, title, start_at, venue, url, city, raw_date_text, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (canon, w[1], w[2], w[3], w[4], w[5], w[6], w[7], w[8]),
        )
    conn.execute(
        """
        INSERT INTO bot_meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (_META_KRAJ_FP_COLLAPSE, _now_utc_iso()),
    )


def _ww_canonical_fingerprint_row(fp: str) -> str | None:
    """Match fingerprint(ev) for wydarzenia.wroclaw.pl after slug stem normalization."""
    if not fp.startswith("ww:"):
        return None
    tail = fp[3:].lstrip("/")
    if not tail:
        return None
    url = "https://wydarzenia.wroclaw.pl/" + tail
    k = _wydarzenia_wroclaw_path_key(url)
    return f"ww:{k}" if k else None


def _collapse_legacy_ww_fingerprints(conn: sqlite3.Connection) -> None:
    """Merge ww:…-2 / ww:…-3 rows into one canonical ww: path (duplicate WordPress permalinks)."""
    if conn.execute(
        "SELECT 1 FROM bot_meta WHERE key=?",
        (_META_WW_FP_COLLAPSE,),
    ).fetchone():
        return
    rows = conn.execute(
        """
        SELECT fingerprint, source_id, title, start_at, venue, url, city, raw_date_text, first_seen_at
        FROM events
        WHERE fingerprint GLOB 'ww:*'
        """
    ).fetchall()
    groups: dict[str, list[tuple]] = {}
    for r in rows:
        fp = r[0]
        canon = _ww_canonical_fingerprint_row(fp)
        if not canon:
            continue
        groups.setdefault(canon, []).append(r)
    for canon, members in groups.items():
        if len(members) == 1 and members[0][0] == canon:
            continue
        members.sort(key=lambda x: x[8])
        w = members[0]
        for r in members:
            conn.execute("DELETE FROM events WHERE fingerprint=?", (r[0],))
        conn.execute(
            """
            INSERT INTO events
              (fingerprint, source_id, title, start_at, venue, url, city, raw_date_text, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (canon, w[1], w[2], w[3], w[4], w[5], w[6], w[7], w[8]),
        )
    conn.execute(
        """
        INSERT INTO bot_meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (_META_WW_FP_COLLAPSE, _now_utc_iso()),
    )


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _start_at_utc_iso(dt: datetime | None) -> str | None:
    """Store start times in UTC so TEXT comparisons used for pruning are chronological."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(SCHEMA)
    _collapse_legacy_go_fingerprints(conn)
    _collapse_legacy_kraj_fingerprints(conn)
    _collapse_legacy_ww_fingerprints(conn)
    _collapse_legacy_xslug_fingerprints(conn)
    conn.commit()
    return conn


def upsert_source_health(
    conn: sqlite3.Connection,
    source_id: str,
    url: str,
    *,
    ok: bool,
    sample_count: int | None = None,
    http_status: int | None = None,
    error_kind: str | None = None,
    error_detail: str | None = None,
) -> None:
    row = conn.execute(
        "SELECT consecutive_failures FROM source_health WHERE source_id=?",
        (source_id,),
    ).fetchone()
    consecutive = int(row[0]) if row else 0
    if ok:
        consecutive = 0
        conn.execute(
            """
            INSERT INTO source_health (source_id, url, last_ok_at, consecutive_failures, last_http_status, last_error_kind, last_error_detail, last_sample_count)
            VALUES (?, ?, ?, 0, ?, NULL, NULL, ?)
            ON CONFLICT(source_id) DO UPDATE SET
              url=excluded.url,
              last_ok_at=excluded.last_ok_at,
              consecutive_failures=0,
              last_http_status=excluded.last_http_status,
              last_error_kind=NULL,
              last_error_detail=NULL,
              last_sample_count=excluded.last_sample_count
            """,
            (source_id, url, _now_utc_iso(), http_status, sample_count),
        )
    else:
        consecutive += 1
        conn.execute(
            """
            INSERT INTO source_health (source_id, url, last_fail_at, consecutive_failures, last_http_status, last_error_kind, last_error_detail, last_sample_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
              url=excluded.url,
              last_fail_at=excluded.last_fail_at,
              consecutive_failures=excluded.consecutive_failures,
              last_http_status=excluded.last_http_status,
              last_error_kind=excluded.last_error_kind,
              last_error_detail=excluded.last_error_detail,
              last_sample_count=excluded.last_sample_count
            """,
            (source_id, url, _now_utc_iso(), consecutive, http_status, error_kind, error_detail, sample_count),
        )


_META_LAST_PRUNE = "last_event_prune_at"


def maybe_delete_past_events(
    conn: sqlite3.Connection,
    *,
    min_interval_seconds: int,
    grace_hours: float,
    now: datetime | None = None,
) -> int | None:
    """Remove rows whose scheduled start is in the past (with grace).

    Returns deleted row count, or None if this call skipped the prune (throttle).
    Rows with ``start_at`` NULL are kept (undated / unknown time).

    Throttling avoids running the DELETE on every bot invocation; when it does
    run, ``idx_events_start_at_not_null`` keeps the lookup index-backed (range
    on ``start_at``), not a full table scan.
    """
    now_utc = now if now is not None else datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    row = conn.execute(
        "SELECT value FROM bot_meta WHERE key = ?",
        (_META_LAST_PRUNE,),
    ).fetchone()
    if row:
        try:
            last = datetime.fromisoformat(row[0])
        except ValueError:
            last = None
        else:
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if (now_utc - last).total_seconds() < float(min_interval_seconds):
                return None

    cutoff = now_utc - timedelta(hours=grace_hours)
    cutoff_iso = cutoff.isoformat()
    cur = conn.execute(
        """
        DELETE FROM events
        WHERE start_at IS NOT NULL
          AND start_at < ?
        """,
        (cutoff_iso,),
    )
    deleted = cur.rowcount
    conn.execute(
        """
        INSERT INTO bot_meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (_META_LAST_PRUNE, now_utc.isoformat()),
    )
    return deleted


def insert_event_if_new(conn: sqlite3.Connection, fingerprint: str, ev: Event) -> bool:
    payload = asdict(ev)
    start_at = _start_at_utc_iso(payload["start_at"])
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO events
          (fingerprint, source_id, title, start_at, venue, url, city, raw_date_text, first_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fingerprint,
            ev.source_id,
            ev.title,
            start_at,
            ev.venue,
            ev.url,
            ev.city,
            ev.raw_date_text,
            _now_utc_iso(),
        ),
    )
    return cur.rowcount == 1


def list_source_health_alerts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT source_id, url, consecutive_failures, last_http_status, last_error_kind, last_error_detail
        FROM source_health
        WHERE consecutive_failures >= 3 OR last_http_status IN (403, 429)
        ORDER BY consecutive_failures DESC, last_http_status DESC
        """
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "source_id": r[0],
                "url": r[1],
                "consecutive_failures": int(r[2] or 0),
                "last_http_status": r[3],
                "last_error_kind": r[4],
                "last_error_detail": r[5],
            }
        )
    return out

