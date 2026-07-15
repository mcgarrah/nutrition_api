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
    gpc = body["gpc"]
    assert gpc["status"] == "ok"
    assert gpc["segments"] == 2
    assert gpc["version"] == "test"
    # The full hierarchy is counted, down to attributes — the fixture's rows.
    assert gpc["counts"] == {
        "segments": 2, "families": 2, "classes": 2,
        "bricks": 3, "attribute_types": 1, "attribute_values": 2,
    }
    assert gpc["scope"].startswith("Food/Beverage")
    assert isinstance(gpc["size_mb"], (int, float))
    assert body["usda_fdc"]["status"] == "ok"
    # The key provisioning travels with the upstream status.
    assert body["usda_fdc"]["key"] in {"configured", "demo", "missing"}
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


# ── /health must be bounded ───────────────────────────────────────────

def test_health_reports_a_stalled_upstream_as_degraded_not_500(monkeypatch, healthy_upstreams):
    """A sick upstream degrades the report; it never fails the endpoint."""
    async def timed_out():
        return {"status": "error", "detail": "timed out after 2.0s"}

    monkeypatch.setattr(off, "check_connectivity", timed_out)

    resp = client.get("/api/v1/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert "timed out" in body["open_food_facts"]["detail"]
    assert body["gpc"]["status"] == "ok"        # the parts that work still work


async def test_health_probes_run_concurrently(monkeypatch, gpc_db):
    """Serial probes would cost the sum of the timeouts, not the max."""
    import asyncio as _asyncio
    import time

    from app.main import health

    async def slow_usda():
        await _asyncio.sleep(0.15)
        return {"status": "ok"}

    async def slow_off():
        await _asyncio.sleep(0.15)
        return {"status": "ok"}

    monkeypatch.setattr(usda_fdc, "check_connectivity", slow_usda)
    monkeypatch.setattr(off, "check_connectivity", slow_off)

    start = time.monotonic()
    await health()
    elapsed = time.monotonic() - start

    assert elapsed < 0.28, f"probes ran serially ({elapsed:.2f}s)"
