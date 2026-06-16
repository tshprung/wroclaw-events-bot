"""User-editable block rules (config/blocklist.yaml) and start-time policy."""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import yaml

from .event_window import resolve_when
from .models import Event

_SPACE = re.compile(r"\s+")


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return _SPACE.sub(" ", s).strip()


def _fold_loc(s: str) -> str:
    return _fold(s).replace("\u0142", "l")


def _norm_frag(value: Any) -> str | None:
    if value is None:
        return None
    s = _fold_loc(str(value).strip())
    return s or None


def _parse_hhmm(value: Any) -> time | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw.lower() in {"off", "none", "null", "false", "0"}:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})$", raw)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return time(h, mi)
    return None


def default_blocklist_path() -> Path:
    return Path(os.environ.get("BLOCKLIST_PATH", "./config/blocklist.yaml"))


@dataclass(frozen=True)
class BlockRule:
    note: str
    url_contains: str | None = None
    title_contains: str | None = None
    title_all: tuple[str, ...] = ()
    venue_contains: str | None = None


@dataclass(frozen=True)
class BlocklistConfig:
    block_starts_before: time | None
    block_starts_after: time | None
    rules: tuple[BlockRule, ...]


def _rule_from_raw(raw: dict[str, Any]) -> BlockRule | None:
    title_all_raw = raw.get("title_all")
    if title_all_raw is None:
        title_all: tuple[str, ...] = ()
    elif isinstance(title_all_raw, str):
        title_all = tuple(x for x in (_norm_frag(title_all_raw),) if x)
    else:
        title_all = tuple(x for x in (_norm_frag(v) for v in title_all_raw) if x)

    url_c = _norm_frag(raw.get("url_contains"))
    title_c = _norm_frag(raw.get("title_contains"))
    venue_c = _norm_frag(raw.get("venue_contains"))
    if not any([url_c, title_c, title_all, venue_c]):
        return None

    note = str(raw.get("note") or raw.get("id") or "").strip() or "(no note)"
    return BlockRule(
        note=note,
        url_contains=url_c,
        title_contains=title_c,
        title_all=title_all,
        venue_contains=venue_c,
    )


def load_blocklist(path: Path | None = None) -> BlocklistConfig:
    p = path or default_blocklist_path()
    if not p.is_file():
        before = _parse_hhmm(os.environ.get("EVENT_BLOCK_START_BEFORE"))
        after = _parse_hhmm(os.environ.get("EVENT_BLOCK_START_AFTER"))
        return BlocklistConfig(block_starts_before=before, block_starts_after=after, rules=())

    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    sched = doc.get("schedule") if isinstance(doc.get("schedule"), dict) else {}
    before = _parse_hhmm(os.environ.get("EVENT_BLOCK_START_BEFORE"))
    after = _parse_hhmm(os.environ.get("EVENT_BLOCK_START_AFTER"))
    if isinstance(sched, dict):
        if before is None:
            before = _parse_hhmm(sched.get("block_starts_before"))
        if after is None:
            after = _parse_hhmm(sched.get("block_starts_after"))

    rules: list[BlockRule] = []
    for raw in doc.get("rules") or []:
        if not isinstance(raw, dict):
            continue
        rule = _rule_from_raw(raw)
        if rule is not None:
            rules.append(rule)

    return BlocklistConfig(block_starts_before=before, block_starts_after=after, rules=tuple(rules))


def rule_matches(ev: Event, rule: BlockRule) -> bool:
    u = _fold_loc(unquote(ev.url or ""))
    t = _fold_loc(ev.title or "")
    v = _fold_loc(ev.venue or "")

    if rule.url_contains and rule.url_contains not in u:
        return False
    if rule.title_contains and rule.title_contains not in t:
        return False
    if rule.title_all and not all(frag in t for frag in rule.title_all):
        return False
    if rule.venue_contains and rule.venue_contains not in v:
        return False
    return True


def block_reason_for_event(ev: Event, cfg: BlocklistConfig) -> str | None:
    for rule in cfg.rules:
        if rule_matches(ev, rule):
            return rule.note
    return None


def blocked_by_schedule(ev: Event, now: datetime, cfg: BlocklistConfig) -> str | None:
    """Return schedule block reason, or None if the event passes the time window."""
    earliest = cfg.block_starts_before
    latest = cfg.block_starts_after
    if earliest is None and latest is None:
        return None
    resolved = resolve_when(ev, now)
    if resolved is None or resolved.tm is None:
        return None
    tm = resolved.tm
    if earliest is not None and tm < earliest:
        return f"starts before {earliest.strftime('%H:%M')}"
    if latest is not None and tm > latest:
        return f"starts after {latest.strftime('%H:%M')}"
    return None


def block_reason(ev: Event, now: datetime, cfg: BlocklistConfig | None = None) -> str | None:
    """Human-readable reason if blocked by user config, else None."""
    cfg = cfg or load_blocklist()
    note = block_reason_for_event(ev, cfg)
    if note:
        return f"blocklist: {note}"
    sched = blocked_by_schedule(ev, now, cfg)
    if sched:
        return f"schedule: {sched}"
    return None


def event_blocked(ev: Event, now: datetime, cfg: BlocklistConfig | None = None) -> bool:
    return block_reason(ev, now, cfg) is not None
