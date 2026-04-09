from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramClient:
    token: str

    def send_message(self, chat_id: str, text: str, *, disable_web_preview: bool = True) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_preview,
        }
        r = requests.post(url, json=payload, timeout=(5, 20))
        if r.status_code >= 400:
            raise RuntimeError(f"Telegram send failed: {r.status_code} {r.text[:300]}")


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

