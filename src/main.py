from __future__ import annotations

import logging
import os
import pathlib
import sqlite3
import uuid
from datetime import datetime, time as dt_time

import requests
import yaml
from dotenv import dotenv_values, load_dotenv
from dateutil import tz

from .config import load_settings
from .event_window import (
    filter_events_in_window,
    format_event_when_display,
    load_window_options,
    resolve_when,
)
from .dedupe import (
    collapse_wroclaw_go_same_detail_url,
    collapse_wroclaw_go_twin_listings,
    fingerprint,
    should_skip_cross_source_duplicate,
)
from .exclusions import filter_out_excluded_events
from .health import should_admin_alert
from .models import Event, Source
from .storage import connect, insert_event_if_new, list_source_health_alerts
from .storage import maybe_delete_past_events, upsert_source_health
from .telegram import TelegramClient, format_event_message
from .sources.common import extract_social_image_url, fetch_facebook_event_search, fetch_url
from .sources.parsers import parser_for_kind


log = logging.getLogger("wroclaw_events_bot")


def _within_allowed_hours(now_local: datetime) -> bool:
    # Allowed: 06:00–22:59 local (inclusive hours 6..22; e.g. cron at 22:05 may run).
    h = now_local.hour
    if h < 6:
        return False
    if h > 22:
        return False
    return True


class _SingleInstanceLock:
    def __init__(self, path: str):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        p = pathlib.Path(self.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            # O_EXCL makes this atomic: if file exists, another run holds the lock.
            self._fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self._fd, str(os.getpid()).encode("utf-8"))
            return True
        except FileExistsError:
            return False

    def release(self) -> None:
        try:
            if self._fd is not None:
                os.close(self._fd)
        finally:
            self._fd = None
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass


def _load_sources(path: str) -> list[Source]:
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    sources = []
    for s in doc.get("sources", []):
        link_limit: int | None = None
        if s.get("link_limit") is not None:
            link_limit = int(s["link_limit"])
        sources.append(
            Source(
                id=str(s["id"]),
                name=str(s.get("name") or s["id"]),
                url=str(s["url"]),
                kind=str(s.get("kind") or "generic_links"),
                enabled=bool(s.get("enabled", True)),
                verify_ssl=bool(s.get("verify_ssl", True)),
                link_limit=link_limit,
            )
        )
    return sources


def _source_config_paths() -> list[pathlib.Path]:
    override = os.environ.get("SOURCES_CONFIG_PATHS", "").strip()
    if override:
        return [pathlib.Path(p.strip()) for p in override.split(os.pathsep) if p.strip()]
    return [
        pathlib.Path("./config/sources_phase1.yaml"),
        pathlib.Path("./config/sources_osiedla.yaml"),
    ]


def _load_all_sources() -> list[Source]:
    """Merge YAML source lists; duplicate `id` values keep the first file’s definition."""
    merged: list[Source] = []
    seen: set[str] = set()
    for path in _source_config_paths():
        if not path.is_file():
            if path.name == "sources_osiedla.yaml":
                continue
            raise RuntimeError(f"Missing sources config: {path}")
        for s in _load_sources(str(path)):
            if s.id in seen:
                log.warning("Duplicate source id %r in %s — skipped", s.id, path)
                continue
            seen.add(s.id)
            merged.append(s)
    return merged


def _short(s: str, n: int = 140) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _format_admin_source_alert_lines(alerts: list[dict], *, limit: int = 20) -> list[str]:
    """Avoid spamming the admin chat when many osiedle.* sources fail for the same host outage."""
    osiedle_block = [
        a
        for a in alerts
        if (a.get("url") or "").lower().startswith("https://osiedle.wroc.pl/")
        and a.get("last_http_status") is None
        and a.get("last_error_kind") == "request_error"
    ]
    os_ids = {a["source_id"] for a in osiedle_block}
    lines: list[str] = []
    rest = [a for a in alerts if a["source_id"] not in os_ids]
    if len(osiedle_block) >= 3:
        mx = max(int(a.get("consecutive_failures") or 0) for a in osiedle_block)
        sample = osiedle_block[0]
        det = (sample.get("last_error_detail") or "").strip()
        tail = f"\nDetail: {_short(det, 220)}" if det else ""
        lines.append(
            "osiedle.wroc.pl (cluster): "
            f"{len(osiedle_block)} sources x request_error (max consecutive failures={mx}); status=None\n"
            f"Example: {sample['source_id']}\n{sample['url']}{tail}\n"
            f"({len(osiedle_block)} osiedle.* sources share one host — outages affect all.)"
        )
    else:
        rest = list(alerts)

    budget = max(0, limit - len(lines))
    for a in rest[:budget]:
        lines.append(
            f"{a['source_id']}: fails={a['consecutive_failures']} status={a['last_http_status']} kind={a['last_error_kind']}\n{a['url']}"
        )
    return lines


def _wroclaw_go_page_url(base: str, page: int) -> str:
    if page <= 1:
        return base
    joiner = "&" if "?" in base else "?"
    return f"{base}{joiner}strona={page}"


def _dedupe_events_by_url(events: list[Event]) -> list[Event]:
    seen: set[str] = set()
    out: list[Event] = []
    for e in events:
        if e.url in seen:
            continue
        seen.add(e.url)
        out.append(e)
    return out


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    load_dotenv()
    # `load_dotenv()` does not override an already-set `DRY_RUN` (e.g. from shell).
    # If the file defines DRY_RUN, treat that as authoritative for posting behavior.
    env_file = pathlib.Path(".env")
    if env_file.is_file():
        file_vals = dotenv_values(env_file) or {}
        if "DRY_RUN" in file_vals and file_vals["DRY_RUN"] is not None:
            os.environ["DRY_RUN"] = str(file_vals["DRY_RUN"]).strip()

    settings = load_settings()
    log.info("DRY_RUN effective=%r dry_run=%s", os.environ.get("DRY_RUN"), settings.dry_run)

    tzinfo = tz.gettz(settings.timezone) or tz.tzlocal()
    now_local = datetime.now(tzinfo)
    if not _within_allowed_hours(now_local):
        log.info("Outside allowed hours (%s). Exiting.", now_local.isoformat())
        return 0

    lock = _SingleInstanceLock("./data/run.lock")
    if not lock.acquire():
        log.warning("Another run is already in progress (lock exists). Exiting.")
        return 0

    run_id = str(uuid.uuid4())
    log.info("Run %s starting at %s", run_id, now_local.isoformat())

    sources = [s for s in _load_all_sources() if s.enabled]
    session = requests.Session()
    conn = connect(settings.db_path)

    telegram: TelegramClient | None = None
    if not settings.dry_run:
        if not settings.telegram_bot_token or not settings.telegram_channel_id:
            raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL_ID (or set DRY_RUN=1).")
        telegram = TelegramClient(settings.telegram_bot_token)

    total_ok = 0
    total_fail = 0
    new_events = 0

    max_posts = int(os.environ.get("MAX_POSTS_PER_RUN", "30"))
    posts_sent = 0
    post_mode = os.environ.get("POST_MODE", "immediate").strip().lower()  # immediate|digest
    # Queue new events and post after sorting (avoid per-source ordering bias).
    # Each entry: (resolved_dt_or_none, msg, Event, verify_ssl)
    post_queue: list[tuple[datetime | None, str, Event, bool]] = []
    window_days, window_include_undated = load_window_options()
    log.info(
        "Event window: days=%d include_undated=%s TIMEZONE=%s (set via EVENT_WINDOW_* / TIMEZONE)",
        window_days,
        window_include_undated,
        settings.timezone,
    )

    try:
        cross_run_seen: list[Event] = []
        for src in sources:
            try:
                parser = parser_for_kind(src.kind)
                events_raw: list[Event] = []
                http_for_health: int = 200

                if src.kind == "wroclaw_go":
                    max_pages = max(1, min(30, int(os.environ.get("WROCLAW_GO_MAX_PAGES", "30"))))
                    failed_first = False
                    for page in range(1, max_pages + 1):
                        page_url = _wroclaw_go_page_url(src.url, page)
                        try:
                            res = fetch_url(session, page_url, verify=src.verify_ssl)
                        except requests.exceptions.RequestException:
                            if page == 1:
                                raise
                            log.info("[%s] stopped pages at %d (request error after first page)", src.id, page)
                            break
                        http_for_health = res.status_code
                        if res.status_code >= 400:
                            if page == 1:
                                upsert_source_health(
                                    conn,
                                    src.id,
                                    src.url,
                                    ok=False,
                                    http_status=res.status_code,
                                    error_kind="http_error",
                                    error_detail=f"HTTP {res.status_code}",
                                )
                                total_fail += 1
                                conn.commit()
                                log.warning("[%s] HTTP %s", src.id, res.status_code)
                                failed_first = True
                            break
                        batch = parser(src, res.text)
                        if not batch:
                            break
                        events_raw.extend(batch)
                    if failed_first:
                        continue
                    events_raw = _dedupe_events_by_url(events_raw)
                    events_raw = collapse_wroclaw_go_twin_listings(events_raw, tzinfo)
                else:
                    if src.kind == "facebook_event_search":
                        res = fetch_facebook_event_search(session, src.url, verify=src.verify_ssl)
                    else:
                        res = fetch_url(session, src.url, verify=src.verify_ssl)
                    http_for_health = res.status_code
                    if res.status_code >= 400:
                        upsert_source_health(
                            conn,
                            src.id,
                            src.url,
                            ok=False,
                            http_status=res.status_code,
                            error_kind="http_error",
                            error_detail=f"HTTP {res.status_code}",
                        )
                        total_fail += 1
                        conn.commit()
                        log.warning("[%s] HTTP %s", src.id, res.status_code)
                        continue
                    events_raw = parser(src, res.text)

                events = filter_events_in_window(events_raw, now_local)
                if len(events) < len(events_raw):
                    log.info(
                        "[%s] time window: kept %d/%d (now .. +%dd)",
                        src.id,
                        len(events),
                        len(events_raw),
                        window_days,
                    )
                events, n_excl = filter_out_excluded_events(events)
                if n_excl:
                    log.info("[%s] exclusions: dropped %d", src.id, n_excl)
                events = collapse_wroclaw_go_same_detail_url(events, tzinfo)
                if (
                    src.kind == "wroclaw_go"
                    and events_raw
                    and not events
                ):
                    s0 = events_raw[0]
                    log.warning(
                        "[%s] wroclaw.pl/go: 0 kept of %d. Example row title=%r raw_date_text=%r resolve_when=%r",
                        src.id,
                        len(events_raw),
                        _short(s0.title, 100),
                        s0.raw_date_text,
                        resolve_when(s0, now_local),
                    )

                upsert_source_health(
                    conn,
                    src.id,
                    src.url,
                    ok=True,
                    http_status=http_for_health,
                    sample_count=len(events),
                )
                total_ok += 1

                new_for_source = 0
                for ev in events:
                    if should_skip_cross_source_duplicate(ev, cross_run_seen, tzinfo):
                        continue
                    fp = fingerprint(ev)
                    if insert_event_if_new(conn, fp, ev):
                        cross_run_seen.append(ev)
                        new_events += 1
                        new_for_source += 1
                        msg = format_event_message(
                            title=ev.title,
                            when=format_event_when_display(ev, tzinfo),
                            venue=ev.venue,
                            url=ev.url,
                        )
                        r = resolve_when(ev, now_local)
                        when_dt: datetime | None = None
                        if r is not None:
                            if r.tm is not None:
                                when_dt = datetime.combine(r.day, r.tm, tzinfo=now_local.tzinfo)
                            else:
                                # Whole-day resolution: order by start-of-day.
                                when_dt = datetime.combine(r.day, dt_time.min, tzinfo=now_local.tzinfo)
                        post_queue.append((when_dt, msg, ev, bool(src.verify_ssl)))

                log.info(
                    "[%s] parsed=%d kept=%d new=%d",
                    src.id,
                    len(events_raw),
                    len(events),
                    new_for_source,
                )

                conn.commit()
            except requests.exceptions.RequestException as e:
                # Network/proxy blocks are common; avoid full stack traces spam.
                upsert_source_health(
                    conn,
                    src.id,
                    src.url,
                    ok=False,
                    error_kind="request_error",
                    error_detail=str(e)[:300],
                )
                conn.commit()
                total_fail += 1
                log.warning("[%s] request error: %s", src.id, str(e)[:220])
            except Exception as e:
                upsert_source_health(
                    conn,
                    src.id,
                    src.url,
                    ok=False,
                    error_kind=type(e).__name__,
                    error_detail=str(e)[:300],
                )
                conn.commit()
                total_fail += 1
                log.exception("[%s] failed: %s", src.id, e)

        def _queue_sort_key(it: tuple[datetime | None, str, Event, bool]) -> tuple[int, datetime]:
            dtv = it[0]
            if dtv is None:
                # Undated last (only if window_include_undated=True).
                return (1, datetime.max.replace(tzinfo=now_local.tzinfo))
            return (0, dtv)

        # Post after sorting by (date,time) across all sources.
        if post_queue:
            post_queue.sort(key=_queue_sort_key)

        if post_mode == "digest" and post_queue:
            sliced = post_queue[:max_posts]
            lines = [msg for _dtv, msg, _ev, _vssl in sliced]
            header = f"New events found: {len(lines)}"
            body = "\n\n".join(lines)
            digest_msg = f"{header}\n\n{body}"
            if settings.dry_run:
                log.info("[DRY_RUN] Digest:\n%s", _short(digest_msg, 2000))
            else:
                assert telegram is not None
                try:
                    telegram.send_message(settings.telegram_channel_id, digest_msg)
                    posts_sent += 1
                except RuntimeError as e:
                    log.error("Telegram digest failed (events are still saved): %s", e)

        if post_mode != "digest" and post_queue:
            for _dtv, msg, ev, vssl in post_queue[:max_posts]:
                if settings.dry_run:
                    log.info("[DRY_RUN] New: %s", _short(msg, 220))
                    continue
                # Safety: never post events that already started.
                # (Some sources embed the date/time in titles; if parsing fails anywhere, this is the last gate.)
                r_now = resolve_when(ev, now_local)
                if r_now is not None and r_now.tm is not None:
                    ev_start = datetime.combine(r_now.day, r_now.tm, tzinfo=now_local.tzinfo)
                    if ev_start <= now_local:
                        log.info(
                            "Skip already-started event (start=%s now=%s) url=%s title=%r",
                            ev_start.isoformat(),
                            now_local.isoformat(),
                            ev.url,
                            _short(ev.title, 120),
                        )
                        continue
                assert telegram is not None
                try:
                    # Best-effort: attach a cover image when the event page exposes one.
                    img_url: str | None = None
                    try:
                        res2 = fetch_url(session, ev.url, verify=vssl)
                        if res2.status_code < 400:
                            img_url = extract_social_image_url(res2.final_url or ev.url, res2.text)
                    except Exception:
                        img_url = None

                    if img_url:
                        telegram.send_photo(settings.telegram_channel_id, img_url, caption=msg)
                    else:
                        telegram.send_message(settings.telegram_channel_id, msg)
                    posts_sent += 1
                except RuntimeError as e:
                    log.error(
                        "Telegram channel post failed (events are still saved): %s",
                        e,
                    )

        # Admin alert on repeated failures/blocks
        alerts = list_source_health_alerts(conn)
        enabled_ids = {s.id for s in sources}
        alerts = [a for a in alerts if a["source_id"] in enabled_ids]
        if alerts:
            lines = _format_admin_source_alert_lines(alerts, limit=20)
            alert_msg = "Source alerts (consider skipping / fixing):\n\n" + "\n\n".join(lines)
            if settings.admin_telegram_id:
                if settings.dry_run:
                    log.warning("[DRY_RUN] Admin alert would be sent:\n%s", _short(alert_msg, 2000))
                else:
                    assert telegram is not None
                    try:
                        telegram.send_message(
                            settings.admin_telegram_id,
                            alert_msg,
                            disable_web_preview=True,
                        )
                    except RuntimeError as e:
                        log.warning(
                            "Admin Telegram alert failed (%s). "
                            "Use your numeric user id, message /start to the bot in private chat, "
                            "or unset ADMIN_TELEGRAM_ID. Alert text:\n%s",
                            e,
                            _short(alert_msg, 2000),
                        )
            else:
                log.warning("%s", alert_msg)

        if os.environ.get("EVENT_PRUNE_DISABLED", "").strip() != "1":
            min_iv = int(os.environ.get("EVENT_PRUNE_MIN_INTERVAL_SEC", "21600"))
            grace_h = float(os.environ.get("EVENT_PRUNE_GRACE_HOURS", "2"))
            try:
                removed = maybe_delete_past_events(
                    conn,
                    min_interval_seconds=max(0, min_iv),
                    grace_hours=max(0.0, grace_h),
                )
            except sqlite3.Error as e:
                log.warning("Past-event DB prune failed: %s", e)
            else:
                if removed is not None:
                    conn.commit()
                    if removed:
                        log.info("Pruned %d past event row(s) from DB (grace=%sh)", removed, grace_h)

        log.info(
            "Run done. sources_ok=%d sources_failed=%d new_events=%d posts_sent=%d",
            total_ok,
            total_fail,
            new_events,
            posts_sent,
        )
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())

