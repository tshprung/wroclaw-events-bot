"""Family preferences shared by classification, caching and notifications."""
from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yaml


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold()).replace("ł", "l")
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


@dataclass(frozen=True)
class FamilyPreferences:
    ages_months: tuple[int, ...]
    nearby_localities: tuple[str, ...]

    @property
    def policy_version(self) -> str:
        payload = json.dumps(["family-v2", self.ages_months, self.nearby_localities])
        return hashlib.sha256(payload.encode()).hexdigest()[:20]

    def allows_locality(self, locality: str | None) -> bool:
        return bool(locality) and fold(locality) in {fold(x) for x in self.nearby_localities}


def load_family_preferences() -> FamilyPreferences:
    path = Path(os.environ.get("FAMILY_CONFIG_PATH", "./config/family.yaml"))
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    ages = tuple(doc["ages_months"])
    towns = tuple(doc["nearby_localities"])
    if not ages or any(type(x) is not int or x < 0 for x in ages):
        raise ValueError("ages_months must contain non-negative integer ages")
    if not towns or any(not isinstance(x, str) or not x.strip() for x in towns):
        raise ValueError("nearby_localities must contain non-empty names")
    return FamilyPreferences(ages, towns)


def age_label(months: int) -> str:
    return f"{months // 12}-year-old" if months % 12 == 0 else f"{months}-month-old"
