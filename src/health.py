from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceFailure:
    source_id: str
    url: str
    http_status: int | None
    error_kind: str
    detail: str | None = None


def should_admin_alert(consecutive_failures: int, http_status: int | None) -> bool:
    # Conservative defaults; tune later.
    if consecutive_failures >= 3:
        return True
    if http_status in (403, 429):
        return True
    return False

