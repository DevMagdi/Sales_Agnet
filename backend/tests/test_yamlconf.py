"""Business configuration must load, validate and cover the seeded playbooks."""

from __future__ import annotations

import pytest

from app.core.errors import ConfigError
from app.core.yamlconf import (
    clear_config_cache,
    load_blocklist,
    load_claims,
    load_holidays,
    load_industries,
)


def test_claims_facts_and_forbidden() -> None:
    claims = load_claims()
    assert claims.product_name == "ProcureAI"
    assert len(claims.facts) >= 11
    ids = {f.id for f in claims.facts}
    assert {"intake", "comparison", "control", "negotiation", "approvals"} <= ids
    assert claims.fact("intake", "ar").startswith("تستقبل")
    assert claims.fact("intake", "en").startswith("Takes")
    assert any("savings" in item for item in claims.forbidden)
    assert claims.status_note


def test_unknown_fact_raises() -> None:
    with pytest.raises(ConfigError):
        load_claims().fact("does_not_exist")


def test_industries_have_required_fields() -> None:
    industries = load_industries()
    codes = industries.codes()
    assert len(codes) >= 10
    for code in (
        "packaging_printing",
        "food_beverage",
        "plastics_rubber",
        "building_materials_steel",
        "textiles_garments",
        "chemicals_paints_detergents",
        "engineering_metal_auto",
        "furniture_wood",
        "electrical_cables",
        "pharma_cosmetics",
    ):
        playbook = industries.by_code(code)
        assert playbook is not None, code
        assert playbook.purchased_categories
        assert playbook.demo_scenario
        assert playbook.example_rfq_items


def test_holidays_per_country() -> None:
    from datetime import date

    for country in ("EG", "SA", "AE"):
        holidays = load_holidays(country)
        assert len(holidays.holidays) >= 8
    eg = load_holidays("EG")
    assert eg.is_holiday(date(2026, 1, 25))
    assert not eg.is_holiday(date(2026, 2, 3))


def test_blocklists() -> None:
    blocklist = load_blocklist()
    assert "coupa.com" in blocklist.competitors
    assert "gmail.com" in blocklist.free_mail
    assert "mailinator.com" in blocklist.disposable
    assert "gov.eg" in blocklist.government


def test_missing_file_raises() -> None:
    with pytest.raises(ConfigError):
        load_holidays("ZZ")


def test_cache_clear_works() -> None:
    load_claims()
    clear_config_cache()
    assert load_claims().product_name == "ProcureAI"
