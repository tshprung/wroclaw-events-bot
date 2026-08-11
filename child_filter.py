from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup

from .models import Event
from .storage import get_child_classification, save_child_classification

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChildDecision:
    relevant: bool
    age_min: int | None
    age_max: int | None
    confidence: float
    reason: str


_INCLUDE_STRONG = (
    "maluch",
    "maluchy",
    "niemowle",
    "niemowl",
    "przedszkol",
    "dla rodzicow z dziecmi",
    "dla rodziców z dziećmi",
    "dla rodzin z dziecmi",
    "dla rodzin z dziećmi",
    "rodzinne",
    "rodzinna",
    "rodzinny",
    "dla calej rodziny",
    "dla całej rodziny",
    "dzieci 0-",
    "dzieci 1-",
    "dzieci 2-",
    "dzieci 3-",
    "dzieci 4-",
    "dzieci 5-",
)

_EXCLUDE_STRONG = (
    "dla doroslych",
    "dla dorosłych",
    "dla seniorow",
    "dla seniorów",
    "dla mlodziezy",
    "dla młodzieży",
    "dla nastolatkow",
    "dla nastolatków",
    "18+",
    "20+",
    "21+",
)

_AGE_RE = re.compile(r"(?:od\s*)?(\d{1,2})\s*(?:-|–|—|do)\s*(\d{1,2})\s*(?:lat|r\.?)?", re.I)
_PLUS_RE = re.compile(r"(?:od\s*)?(\d{1,2})\s*\+", re.I)


def _fold(text: str) -> str:
    s = unicodedata.normalize("NFKD", text or "").casefold()
    return "".join(c for c in s if not unicodedata.combining(c))


def _extract_age_range(text: str) -> tuple[int | None, int | None]:
    folded = _fold(text)
    ranges = []
    for m in _AGE_RE.finditer(folded):
        a, b = int(m.group(1)), int(m.group(2))
        if 0 <= a <= 18 and 0 <= b <= 18:
            ranges.append((min(a, b), max(a, b)))
    if ranges:
        return min(x[0] for x in ranges), max(x[1] for x in ranges)
    plus = _PLUS_RE.search(folded)
    if plus:
        return int(plus.group(1)), None
    return None, None


def _heuristic(event: Event) -> ChildDecision | None:
    text = _fold(" ".join((event.title or "", event.venue or "", unquote(event.url or ""))))
    age_min, age_max = _extract_age_range(text)

    if age_min is not None and age_min >= 7:
        return ChildDecision(False, age_min, age_max, 0.99, "Explicit minimum age is 7+")
    if age_max is not None and age_max <= 6:
        return ChildDecision(True, age_min, age_max, 0.99, "Explicit age range is within 0-6")
    if any(x in text for x in _EXCLUDE_STRONG):
        return ChildDecision(False, age_min, age_max, 0.98, "Event is explicitly for adults, seniors, or teenagers")
    if any(x in text for x in _INCLUDE_STRONG):
        return ChildDecision(True, age_min, age_max, 0.88, "Strong family/young-child indicator")
    return None


def _page_text(html: str, max_chars: int = 7000) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        tag.decompose()
    text = " ".join(soup.stripped_strings)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _fetch_event_context(session: requests.Session, event: Event, verify_ssl: bool) -> str:
    try:
        res = session.get(
            event.url,
            timeout=(8, 25),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
                "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
            },
            allow_redirects=True,
            verify=verify_ssl,
        )
        if res.status_code >= 400:
            return ""
        return _page_text(res.text)
    except requests.RequestException:
        return ""


def _openai_classify(event: Event, page_text: str) -> ChildDecision | None:
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return None

    model = (os.environ.get("CHILD_FILTER_MODEL") or "gpt-5-mini").strip()
    context = page_text or "(event page could not be fetched)"
    prompt = f"""Classify this Wrocław event for a Telegram channel aimed at parents of children aged 0-6.

Include an event if a parent with a child aged 0-6 would realistically consider attending it with the child.
Exclude events primarily aimed at adults, teenagers, school-age children 7+, professional audiences, or formal meetings.
Family events can be included even when adults also attend. If the event is suitable for young children but the exact age is unclear, include it only when the description gives a credible reason.
Do not infer suitability merely because the word 'children' appears.

Return ONLY valid JSON with exactly these keys:
relevant (boolean), age_min (integer or null), age_max (integer or null), confidence (number 0..1), reason (short string).

TITLE: {event.title}
VENUE: {event.venue or ''}
DATE: {event.raw_date_text or (event.start_at.isoformat() if event.start_at else '')}
URL: {event.url}
PAGE TEXT:
{context}
"""

    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": "You are a strict event relevance classifier. The requested audience is children aged 0-6 and their parents."},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=(10, 45),
        )
        r.raise_for_status()
        body = r.json()
        content = body["choices"][0]["message"]["content"]
        data = json.loads(content)
        relevant = bool(data.get("relevant"))
        age_min = data.get("age_min")
        age_max = data.get("age_max")
        confidence = float(data.get("confidence", 0.0))
        reason = str(data.get("reason") or "LLM classification")[:500]
        if age_min is not None:
            age_min = int(age_min)
        if age_max is not None:
            age_max = int(age_max)
        return ChildDecision(relevant, age_min, age_max, max(0.0, min(1.0, confidence)), reason)
    except (requests.RequestException, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        log.warning("Child-event LLM classification failed for %s: %s", event.url, exc)
        return None


def classify_event(
    conn,
    session: requests.Session,
    event: Event,
    fingerprint: str,
    *,
    verify_ssl: bool = True,
) -> ChildDecision:
    cached = get_child_classification(conn, fingerprint)
    if cached is not None:
        return ChildDecision(
            bool(cached["relevant"]),
            cached["age_min"],
            cached["age_max"],
            float(cached["confidence"] or 0.0),
            str(cached["reason"] or "cached"),
        )

    decision = _heuristic(event)
    if decision is None:
        page_text = _fetch_event_context(session, event, verify_ssl)
        decision = _openai_classify(event, page_text)

    if decision is None:
        # When no API key is configured (or the API fails), fail closed rather than
        # flooding the channel with events that have not passed the child filter.
        decision = ChildDecision(False, None, None, 0.0, "No reliable child-age classification available")

    save_child_classification(
        conn,
        fingerprint,
        relevant=decision.relevant,
        age_min=decision.age_min,
        age_max=decision.age_max,
        confidence=decision.confidence,
        reason=decision.reason,
    )
    return decision
