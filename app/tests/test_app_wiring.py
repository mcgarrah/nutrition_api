"""
Tests for application wiring: the startup lifespan, the database singleton,
CORS, the static UI mount, and the OpenAPI surface.

The lifespan is the riskiest part — it shells out to the GPC importer on every
boot. Its contract is that a *missing* database is fatal (we cannot serve
taxonomy without one) but a *failed update* of an existing database is not:
the app must come up on the data it already has.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import subprocess

import pytest
from fastapi.testclient import TestClient

import app.database as database
from app.main import app, lifespan

client = TestClient(app)


# ── Startup lifespan ──────────────────────────────────────────────────

@pytest.fixture
def fake_subprocess(monkeypatch):
    """Capture the importer invocations the lifespan makes."""
    calls = []

    def record(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("subprocess.run", record)
    return calls


async def test_missing_database_is_built_on_startup(monkeypatch, fake_subprocess, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "absent.sqlite3")

    async with lifespan(app):
        pass

    cmd, kwargs = fake_subprocess[0]
    assert "import_gpc_xml.py" in cmd[1]
    assert "--auto-update" not in cmd       # a full build, not an update
    assert kwargs["check"] is True          # a failed build must be fatal


async def test_existing_database_triggers_an_auto_update(monkeypatch, fake_subprocess, gpc_db):
    async with lifespan(app):
        pass

    cmd, kwargs = fake_subprocess[0]
    assert "--auto-update" in cmd
    assert kwargs["timeout"] == 120         # never block boot indefinitely


async def test_startup_survives_an_update_timeout(monkeypatch, gpc_db):
    """GS1 being slow must not stop the app from serving existing data."""
    def timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 120)

    monkeypatch.setattr("subprocess.run", timeout)

    async with lifespan(app):
        pass  # must not raise


async def test_startup_survives_a_failing_update(monkeypatch, gpc_db):
    """A non-zero exit from the importer must not stop the app."""
    def fails(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr("subprocess.run", fails)

    async with lifespan(app):
        pass  # must not raise


async def test_startup_propagates_a_failed_initial_build(monkeypatch, tmp_path):
    """With no database at all there is nothing to degrade to — fail loudly."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "absent.sqlite3")

    def fails(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr("subprocess.run", fails)

    with pytest.raises(subprocess.CalledProcessError):
        async with lifespan(app):
            pass


# ── Database singleton ────────────────────────────────────────────────

async def test_get_db_returns_the_same_connection(gpc_db):
    first = await database.get_db()
    assert await database.get_db() is first


async def test_close_db_releases_the_connection(gpc_db):
    await database.get_db()
    assert database._db is not None

    await database.close_db()
    assert database._db is None


async def test_close_db_is_idempotent(gpc_db):
    await database.close_db()
    await database.close_db()   # must not raise on an already-closed handle


async def test_rows_are_index_and_name_addressable(gpc_db):
    """row_factory is set to aiosqlite.Row — routes index by position."""
    db = await database.get_db()
    rows = await db.execute_fetchall("SELECT segment_code, description FROM segments LIMIT 1")
    assert rows[0][0] == "50000000"
    assert rows[0]["description"] == "Food/Beverage"


# ── CORS ──────────────────────────────────────────────────────────────

def test_cors_allows_cross_origin_reads():
    resp = client.get("/api/v1/version", headers={"Origin": "https://example.com"})
    assert resp.headers["access-control-allow-origin"] == "*"


def test_cors_preflight_permits_get():
    resp = client.options(
        "/api/v1/version",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200
    assert "GET" in resp.headers["access-control-allow-methods"]


# ── Static UI ─────────────────────────────────────────────────────────

def test_root_serves_the_routing_landing_page():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Nutrition API" in resp.text
    # The two lookup types are the choice this page exists to route between.
    assert "/lookup" in resp.text
    assert "/search" in resp.text


def test_lookup_serves_the_barcode_tester_ui():
    resp = client.get("/lookup")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Lookup Tester" in resp.text


def test_gpc_browser_is_served():
    resp = client.get("/gpc")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "GPC Browser" in resp.text


def test_static_pages_are_excluded_from_the_openapi_schema():
    paths = client.get("/openapi.json").json()["paths"]
    for page in ("/", "/lookup", "/gpc", "/data", "/search", "/gpc/mappings"):
        assert page not in paths


# ── OpenAPI surface ───────────────────────────────────────────────────

def test_openapi_documents_every_public_route():
    paths = client.get("/openapi.json").json()["paths"]
    for expected in [
        "/api/v1/lookup/{gtin}",
        "/api/v1/health",
        "/api/v1/version",
        "/api/v1/usda/search",
        "/api/v1/off/product/{barcode}",
        "/api/v1/gpc/segments/",
        "/api/v1/gpc/search/",
    ]:
        assert expected in paths, f"{expected} missing from OpenAPI"


def test_lookup_response_is_documented_as_canonical_product():
    spec = client.get("/openapi.json").json()
    schema = (
        spec["paths"]["/api/v1/lookup/{gtin}"]["get"]["responses"]["200"]
        ["content"]["application/json"]["schema"]
    )
    assert schema["$ref"].endswith("CanonicalProduct")


def test_docs_endpoints_are_served():
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
