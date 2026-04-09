from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    url: str
    kind: str
    enabled: bool = True


@dataclass(frozen=True)
class Event:
    source_id: str
    title: str
    start_at: datetime | None
    venue: str | None
    url: str
    city: str = "Wrocław"
    raw_date_text: str | None = None

