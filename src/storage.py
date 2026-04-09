from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone

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


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(SCHEMA)
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


def insert_event_if_new(conn: sqlite3.Connection, fingerprint: str, ev: Event) -> bool:
    payload = asdict(ev)
    start_at = payload["start_at"].isoformat() if payload["start_at"] else None
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

