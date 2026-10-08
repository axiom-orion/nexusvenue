"""The showcase site's data is generated from this repo's own code. These tests
keep it honest: web/data.js must match what the code produces today, and the
claims the page makes about itself must hold."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def exporter():
    spec = importlib.util.spec_from_file_location("export_showcase_data", ROOT / "scripts" / "export_showcase_data.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def data(exporter):
    return exporter.build()


def test_web_data_is_current(exporter, data):
    on_disk = (ROOT / "web" / "data.js").read_text(encoding="utf-8")
    assert on_disk == exporter.render(data), (
        "web/data.js is stale. Regenerate it: python scripts/export_showcase_data.py")


def test_export_is_deterministic(exporter, data):
    assert exporter.render(exporter.build()) == exporter.render(data)


def test_page_claims_hold(data):
    # the resolution section leads with "precision comes first"
    assert data["resolution"]["false_merges"] == 0
    # every blueprint citation was actually retrieved (also asserted at build time)
    for rfp in data["rfps"]:
        retrieved = {h["beo_id"] for h in rfp["context"]["similar_past_events"]}
        bp = rfp["blueprint"]
        cited = {b for p in bp["recommended_packages"] for b in p["supporting_beo_ids"]}
        cited |= {e["beo_id"] for e in bp["historical_evidence"]}
        assert cited <= retrieved
    # a second sync is a no-op, the sync section's headline claim
    assert data["delta"]["report"][-1].startswith("up to date")
    # the gold marks on the page come from the generator's relevance set
    assert set(data["_gold"]) == {r["key"] for r in data["rfps"]}
