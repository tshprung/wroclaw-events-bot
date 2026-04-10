from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

_last_send_mono: float = 0.0


@dataclass(frozen=True)
class TelegramClient:
    token: str

    def send_message(self, chat_id: str, text: str, *, disable_web_preview: bool = True) -> None:
        global _last_send_mono
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_preview,
        }
        interval = float(os.environ.get("TELEGRAM_SEND_INTERVAL_SEC", "0.55"))
        gap = interval - (time.monotonic() - _last_send_mono)
        if gap > 0:
            time.sleep(gap)

        last_body: str | None = None
        for attempt in range(8):
            r = requests.post(url, json=payload, timeout=(5, 25))
            if r.status_code == 429:
                try:
                    body = r.json()
                    ra = int(body.get("parameters", {}).get("retry_after", 35))
                except (TypeError, ValueError, json.JSONDecodeError):
                    ra = 35
                wait_s = max(1, min(120, ra)) + 0.75
                log.warning(
                    "Telegram rate limit (429), sleeping %.1fs then retry %d/8",
                    wait_s,
                    attempt + 1,
                )
                time.sleep(wait_s)
                continue
            if r.status_code >= 400:
                last_body = f"{r.status_code} {r.text[:400]}"
                log.warning("Telegram send HTTP error: %s", last_body)
                raise RuntimeError(f"Telegram send failed: {last_body}")
            _last_send_mono = time.monotonic()
            return

        raise RuntimeError(
            f"Telegram send failed after retries (429): {last_body or r.text[:300]}"
        )

    def send_photo(self, chat_id: str, photo_url: str, *, caption: str | None = None) -> None:
        global _last_send_mono
        url = f"https://api.telegram.org/bot{self.token}/sendPhoto"
        payload = {
            "chat_id": chat_id,
            "photo": photo_url,
        }
        if caption:
            # Telegram caption limit is 1024 chars for photos.
            payload["caption"] = caption[:1024]

        interval = float(os.environ.get("TELEGRAM_SEND_INTERVAL_SEC", "0.55"))
        gap = interval - (time.monotonic() - _last_send_mono)
        if gap > 0:
            time.sleep(gap)

        last_body: str | None = None
        for attempt in range(8):
            r = requests.post(url, json=payload, timeout=(5, 25))
            if r.status_code == 429:
                try:
                    body = r.json()
                    ra = int(body.get("parameters", {}).get("retry_after", 35))
                except (TypeError, ValueError, json.JSONDecodeError):
                    ra = 35
                wait_s = max(1, min(120, ra)) + 0.75
                log.warning(
                    "Telegram rate limit (429), sleeping %.1fs then retry %d/8",
                    wait_s,
                    attempt + 1,
                )
                time.sleep(wait_s)
                continue
            if r.status_code >= 400:
                last_body = f"{r.status_code} {r.text[:400]}"
                log.warning("Telegram send HTTP error: %s", last_body)
                raise RuntimeError(f"Telegram send failed: {last_body}")
            _last_send_mono = time.monotonic()
            return

        raise RuntimeError(
            f"Telegram send failed after retries (429): {last_body or r.text[:300]}"
        )


def format_event_message(title: str, when: str | None, venue: str | None, url: str) -> str:
    parts: list[str] = [title]
    meta: list[str] = []
    if when:
        meta.append(when)
    if venue:
        meta.append(venue)
    if meta:
        parts.append(" — ".join(meta))
    parts.append(url)
    return "\n".join(parts)
