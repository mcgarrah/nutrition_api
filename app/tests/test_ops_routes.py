"""
Tests for the operations endpoints: /api/v1/health and /api/v1/version.

Upstream connectivity checks are monkeypatched; the GPC portion of the
health check runs against the shared fixture database.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import pytest
from fastapi.testclient import TestClient

from app.core import open_food_facts as off
from app.core import usda_fdc
from app.main import app

client = TestClient(app)

# The health check reads the GPC database
pytestmark = pytest.mark.usefixtures("gpc_db")


@pytest.fixture
def healthy_upstreams(monkeypatch):
    async def usda_ok():
        return {"status": "ok", "total_foods": 1}

    async def off_ok():
        return {"status": "ok"}

    monkeypatch.setattr(usda_fdc, "check_connectivity", usda_ok)
    monkeypatch.setattr(off, "check_connectivity", off_ok)


def test_health_ok_when_all_sources_up(healthy_upstreams):
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["gpc"] == {
        "status": "ok",
        "segments": 2,
        "version": "test",
        "xml_date": "2026-01-01",
        "import_timestamp": "2026-01-01T00:00:00",
    }
    assert body["usda_fdc"]["status"] == "ok"
    assert body["open_food_facts"]["status"] == "ok"


def test_health_degraded_when_upstream_errors(monkeypatch, healthy_upstreams):
    async def off_down():
        return {"status": "error", "detail": "connection refused"}

    monkeypatch.setattr(off, "check_connectivity", off_down)

    body = client.get("/api/v1/health").json()
    assert body["status"] == "degraded"
    assert body["open_food_facts"]["status"] == "error"
    # Other sources still report independently
    assert body["gpc"]["status"] == "ok"
    assert body["usda_fdc"]["status"] == "ok"


def test_health_unconfigured_usda_is_not_degraded(monkeypatch, healthy_upstreams):
    """A missing API key is a config state, not an outage."""
    async def usda_unconfigured():
        return {"status": "unconfigured", "detail": "FDC_API_KEY not set"}

    monkeypatch.setattr(usda_fdc, "check_connectivity", usda_unconfigured)

    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["usda_fdc"]["status"] == "unconfigured"


def test_version_reports_git_hash(monkeypatch):
    monkeypatch.setenv("GIT_HASH", "abc1234")
    body = client.get("/api/v1/version").json()
    assert body == {"version": "0.1.0", "git_hash": "abc1234"}


def test_version_defaults_to_dev(monkeypatch):
    monkeypatch.delenv("GIT_HASH", raising=False)
    body = client.get("/api/v1/version").json()
    assert body["git_hash"] == "dev"


def test_health_degraded_when_gpc_database_is_broken(monkeypatch, healthy_upstreams):
    """A corrupt/missing GPC database must degrade, not 500."""
    import app.database as database

    async def broken():
        raise RuntimeError("no such table: segments")

    monkeypatch.setattr(database, "get_db", broken)

    resp = client.get("/api/v1/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["gpc"]["status"] == "error"
    assert "no such table" in body["gpc"]["detail"]
