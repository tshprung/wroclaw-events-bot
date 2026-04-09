from __future__ import annotations

import logging
import os
import pathlib
import uuid
from datetime import datetime

import requests
import yaml
from dotenv import load_dotenv
from dateutil import tz

from .config import load_settings
from .dedupe import fingerprint
from .health import should_admin_alert
from .models import Event, Source
from .storage import connect, insert_event_if_new, upsert_source_health
from .storage import list_source_health_alerts
from .telegram import TelegramClient, format_event_message
from .sources.common import fetch_url
from .sources.parsers import parser_for_kind


log = logging.getLogger("wroclaw_events_bot")


def _within_allowed_hours(now_local: datetime) -> bool:
    # Allowed: 06:00 <= time <= 21:00 (inclusive start; inclusive end hour, run at 21:00)
    h = now_local.hour
    if h < 6:
        return False
    if h > 21:
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
        sources.append(
            Source(
                id=str(s["id"]),
                name=str(s.get("name") or s["id"]),
                url=str(s["url"]),
                kind=str(s.get("kind") or "generic_links"),
                enabled=bool(s.get("enabled", True)),
            )
        )
    return sources


def _short(s: str, n: int = 140) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    load_dotenv()
    settings = load_settings()

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

    sources = [s for s in _load_sources("./config/sources_phase1.yaml") if s.enabled]
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
    digest_lines: list[str] = []

    try:
        for src in sources:
            try:
                res = fetch_url(session, src.url)
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
                    log.warning("[%s] HTTP %s", src.id, res.status_code)
                    continue

                parser = parser_for_kind(src.kind)
                events = parser(src, res.text)

                upsert_source_health(
                    conn,
                    src.id,
                    src.url,
                    ok=True,
                    http_status=res.status_code,
                    sample_count=len(events),
                )
                total_ok += 1

                for ev in events:
                    fp = fingerprint(ev)
                    if insert_event_if_new(conn, fp, ev):
                        new_events += 1
                        msg = format_event_message(
                            title=ev.title,
                            when=ev.raw_date_text,
                            venue=ev.venue,
                            url=ev.url,
                        )
                        if post_mode == "digest":
                            if len(digest_lines) < max_posts:
                                digest_lines.append(msg)
                            continue

                        if posts_sent >= max_posts:
                            continue
                        if settings.dry_run:
                            log.info("[DRY_RUN] New: %s", _short(msg, 220))
                        else:
                            assert telegram is not None
                            telegram.send_message(settings.telegram_channel_id, msg)
                        posts_sent += 1

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

        # Post digest if enabled
        if post_mode == "digest" and digest_lines:
            header = f"New events found: {len(digest_lines)}"
            body = "\n\n".join(digest_lines)
            digest_msg = f"{header}\n\n{body}"
            if settings.dry_run:
                log.info("[DRY_RUN] Digest:\n%s", _short(digest_msg, 2000))
            else:
                assert telegram is not None
                telegram.send_message(settings.telegram_channel_id, digest_msg)
                posts_sent += 1

        # Admin alert on repeated failures/blocks
        alerts = list_source_health_alerts(conn)
        if alerts:
            lines = []
            for a in alerts[:20]:
                lines.append(
                    f"{a['source_id']}: fails={a['consecutive_failures']} status={a['last_http_status']} kind={a['last_error_kind']}\n{a['url']}"
                )
            alert_msg = "Source alerts (consider skipping / fixing):\n\n" + "\n\n".join(lines)
            if settings.admin_telegram_id:
                if settings.dry_run:
                    log.warning("[DRY_RUN] Admin alert would be sent:\n%s", _short(alert_msg, 2000))
                else:
                    assert telegram is not None
                    telegram.send_message(settings.admin_telegram_id, alert_msg, disable_web_preview=True)
            else:
                log.warning("%s", alert_msg)

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

