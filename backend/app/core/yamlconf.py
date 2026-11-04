"""Typed loaders for the YAML configuration that the business owns (claims, playbooks, calendars).

Files live in ``config/``. They are the single source of truth for product claims, industry
playbooks, holidays and domain blocklists — nothing about the product is hard-coded in Python.
"""

from __future__ import annotations

import functools
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from app.core.errors import ConfigError


def config_dir() -> Path:
    """Resolve the repository ``config/`` directory (also works inside the container)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "config"
        if (candidate / "claims.yaml").exists():
            return candidate
    raise ConfigError("config/ directory with claims.yaml not found")


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"config file must contain a mapping: {path}")
    return data


class ClaimFact(BaseModel):
    id: str
    ar: str
    en: str


class ClaimsConfig(BaseModel):
    product_name: str
    company: str
    facts: list[ClaimFact]
    status_note: str = ""
    forbidden: list[str] = Field(default_factory=list)
    owner_fill_in: dict[str, Any] = Field(default_factory=dict)

    def fact(self, fact_id: str, lang: str = "en") -> str:
        for item in self.facts:
            if item.id == fact_id:
                return item.ar if lang == "ar" else item.en
        raise ConfigError(f"unknown claim fact id: {fact_id}")

    def all_text(self) -> list[str]:
        out: list[str] = []
        for item in self.facts:
            out.extend([item.ar, item.en])
        return out


class IndustryPlaybook(BaseModel):
    code: str
    name_en: str
    name_ar: str
    purchased_categories: list[str]
    pain_hypotheses: list[str]
    demo_scenario: str
    example_rfq_items: list[str] = Field(default_factory=list)
    typical_zones: list[str] = Field(default_factory=list)
    regulated: bool = False


class IndustriesConfig(BaseModel):
    industries: list[IndustryPlaybook]

    def by_code(self, code: str) -> IndustryPlaybook | None:
        for item in self.industries:
            if item.code == code:
                return item
        return None

    def codes(self) -> list[str]:
        return [i.code for i in self.industries]


class Holiday(BaseModel):
    date: date
    name_en: str
    name_ar: str


class HolidaysConfig(BaseModel):
    country: str
    holidays: list[Holiday]

    def dates(self) -> set[date]:
        return {h.date for h in self.holidays}

    def is_holiday(self, day: date) -> bool:
        return day in self.dates()


class BlocklistConfig(BaseModel):
    competitors: list[str] = Field(default_factory=list)
    government: list[str] = Field(default_factory=list)
    free_mail: list[str] = Field(default_factory=list)
    disposable: list[str] = Field(default_factory=list)

    @field_validator("competitors", "government", "free_mail", "disposable")
    @classmethod
    def _lower(cls, v: list[str]) -> list[str]:
        return [item.strip().lower() for item in v if item.strip()]

    def all_blocked_domains(self) -> set[str]:
        return set(self.competitors) | set(self.government)


@functools.lru_cache(maxsize=8)
def load_claims(path: str | None = None) -> ClaimsConfig:
    target = Path(path) if path else config_dir() / "claims.yaml"
    return ClaimsConfig.model_validate(_read_yaml(target))


@functools.lru_cache(maxsize=8)
def load_industries(path: str | None = None) -> IndustriesConfig:
    target = Path(path) if path else config_dir() / "industries.yaml"
    return IndustriesConfig.model_validate(_read_yaml(target))


@functools.lru_cache(maxsize=8)
def load_blocklist(path: str | None = None) -> BlocklistConfig:
    target = Path(path) if path else config_dir() / "blocklist_domains.yaml"
    return BlocklistConfig.model_validate(_read_yaml(target))


@functools.lru_cache(maxsize=16)
def load_holidays(country: str = "EG", path: str | None = None) -> HolidaysConfig:
    target = Path(path) if path else config_dir() / f"holidays_{country.lower()}.yaml"
    return HolidaysConfig.model_validate(_read_yaml(target))


def clear_config_cache() -> None:
    load_claims.cache_clear()
    load_industries.cache_clear()
    load_blocklist.cache_clear()
    load_holidays.cache_clear()
