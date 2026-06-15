#!/usr/bin/env python3
"""Manage config/blocklist.yaml — list rules, test URLs, add patterns."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dateutil import tz as dttz

from src.blocklist import (
    BlockRule,
    block_reason,
    default_blocklist_path,
    load_blocklist,
)
from src.models import Event


def _print(s: str) -> None:
    try:
        print(s)
    except UnicodeEncodeError:
        print(s.encode("ascii", errors="replace").decode("ascii"))


def _local_now() -> datetime:
    z = dttz.gettz(__import__("os").environ.get("TIMEZONE", "Europe/Warsaw"))
    return datetime.now(z or dttz.tzlocal())


def cmd_list(_: argparse.Namespace) -> int:
    cfg = load_blocklist()
    path = default_blocklist_path()
    print(f"Blocklist: {path.resolve()}")
    if cfg.block_starts_after is not None:
        _print(f"Schedule: drop events starting after {cfg.block_starts_after.strftime('%H:%M')} local")
    else:
        _print("Schedule: no start-time cutoff")
    print()
    if not cfg.rules:
        print("(no pattern rules)")
        return 0
    for i, rule in enumerate(cfg.rules, 1):
        _print(f"{i}. {rule.note}")
        if rule.url_contains:
            _print(f"   url_contains: {rule.url_contains}")
        if rule.title_contains:
            _print(f"   title_contains: {rule.title_contains}")
        if rule.title_all:
            _print(f"   title_all: {', '.join(rule.title_all)}")
        if rule.venue_contains:
            _print(f"   venue_contains: {rule.venue_contains}")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    now = _local_now()
    cfg = load_blocklist()
    ev = Event(
        source_id="test",
        title=args.title or "",
        start_at=None,
        venue=args.venue,
        url=args.url,
        raw_date_text=args.when,
    )
    reason = block_reason(ev, now, cfg)
    if reason:
        _print(f"BLOCKED — {reason}")
        return 0
    _print("OK — would be posted (other bot filters may still apply)")
    return 0


def _append_rule(path: Path, rule: BlockRule) -> None:
    import yaml

    if path.is_file():
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    else:
        doc = {
            "version": 1,
            "schedule": {"block_starts_after": "18:05"},
            "rules": [],
        }
    rules = list(doc.get("rules") or [])
    entry: dict[str, object] = {"note": rule.note}
    if rule.url_contains:
        entry["url_contains"] = rule.url_contains
    if rule.title_contains:
        entry["title_contains"] = rule.title_contains
    if rule.title_all:
        entry["title_all"] = list(rule.title_all)
    if rule.venue_contains:
        entry["venue_contains"] = rule.venue_contains
    rules.append(entry)
    doc["rules"] = rules
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def cmd_add(args: argparse.Namespace) -> int:
    note = args.note or "User block"
    title_all = tuple(args.title_all or [])
    if not any([args.url_contains, args.title_contains, title_all, args.venue_contains]):
        print("Provide at least one of: --url-contains, --title-contains, --title-all, --venue-contains", file=sys.stderr)
        return 2
    rule = BlockRule(
        note=note,
        url_contains=args.url_contains,
        title_contains=args.title_contains,
        title_all=title_all,
        venue_contains=args.venue_contains,
    )
    path = default_blocklist_path()
    _append_rule(path, rule)
    print(f"Added rule to {path.resolve()}")
    print(f"  {note}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage event blocklist (config/blocklist.yaml)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="Show schedule and pattern rules")
    p_list.set_defaults(func=cmd_list)

    p_test = sub.add_parser("test", help="Check whether an event would be blocked")
    p_test.add_argument("--url", required=True)
    p_test.add_argument("--title", default="")
    p_test.add_argument("--venue", default=None)
    p_test.add_argument("--when", default=None, help="raw_date_text, e.g. 06.06.2026 20:00")
    p_test.set_defaults(func=cmd_test)

    p_add = sub.add_parser("add", help="Append a pattern rule")
    p_add.add_argument("--note", default=None)
    p_add.add_argument("--url-contains", default=None)
    p_add.add_argument("--title-contains", default=None)
    p_add.add_argument("--title-all", action="append", default=None)
    p_add.add_argument("--venue-contains", default=None)
    p_add.set_defaults(func=cmd_add)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
