"""
Tests for upstream payloads that don't match their expected shape.

Pydantic does not validate on assignment, so `product.allergens = None` is
accepted silently and only surfaces when FastAPI serializes the response —
by which point the model's type annotations have become decorative. Anything
the orchestrator copies out of an upstream payload must therefore be coerced
on the way in, or the published contract is a suggestion rather than a
guarantee.

Open Food Facts is the realistic source of this: its records are sparse and
user-contributed, so a key is routinely present-but-null.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core import open_food_facts as off
from app.core import orchestrator, usda_fdc
from app.core.models import CanonicalProduct
from app.core.orchestrator import _str_list, _text
from app.main import app

client = TestClient(app)


def patch_upstreams(monkeypatch, off_data=None, usda_data=None):
    async def fake_off(barcode):
        return off_data

    async def fake_usda(upc):
        return usda_data

    async def no_gpc(categories):
        return [], 0.0

    monkeypatch.setattr(off, "get_product", fake_off)
    monkeypatch.setattr(usda_fdc, "search_by_upc", fake_usda)
    monkeypatch.setattr(orchestrator, "_fetch_gpc_categories", no_gpc)


# ── coercion helpers ──────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("Nutella", "Nutella"),
    ("  spaced  ", "  spaced  "),   # preserved; only *blank* is rejected
    ("", None),
    ("   ", None),
    (None, None),
    (123, None),
    ({"not": "text"}, None),
    ([], None),
    (True, None),
])
def test_text_accepts_only_real_text(value, expected):
    assert _text(value) == expected


@pytest.mark.parametrize("value,expected", [
    (["en:milk", "en:nuts"], ["en:milk", "en:nuts"]),
    ([], []),
    (None, []),                     # the OFF null case
    ("en:milk", []),                # a bare string is not a list
    (["en:milk", None, 42], ["en:milk"]),   # drop non-string members
    ({"en:milk": True}, []),
])
def test_str_list_always_returns_a_list_of_strings(value, expected):
    assert _str_list(value) == expected


# ── the contract, end to end ──────────────────────────────────────────

async def test_null_tag_lists_serialize_as_empty_lists(monkeypatch):
    """The documented contract is that these are always iterable. A null here
    breaks every consumer that loops them without a None check."""
    patch_upstreams(monkeypatch, off_data={
        "product_name": "Odd Product",
        "allergens": None,
        "labels": None,
        "categories": None,
        "nutrients_per_100g": {},
    })

    body = client.get("/api/v1/lookup/028400642255").json()

    assert body["allergens"] == []
    assert body["labels"] == []
    assert body["category_hierarchy"] == []


async def test_non_string_image_url_is_rejected(monkeypatch):
    """A dict assigned to image_url survived into the response — Pydantic only
    warned at serialization time."""
    patch_upstreams(monkeypatch, off_data={
        "product_name": "Odd Product",
        "image_url": {"not": "a url"},
        "nutrients_per_100g": {},
        "categories": [],
    })

    body = client.get("/api/v1/lookup/028400642255").json()
    assert body["image_url"] is None


async def test_non_string_tag_members_are_dropped(monkeypatch):
    patch_upstreams(monkeypatch, off_data={
        "product_name": "Odd Product",
        "allergens": ["en:milk", None, 42, {"x": 1}],
        "nutrients_per_100g": {},
        "categories": [],
    })

    body = client.get("/api/v1/lookup/028400642255").json()
    assert body["allergens"] == ["en:milk"]


async def test_blank_product_name_falls_back_to_the_default(monkeypatch):
    patch_upstreams(monkeypatch, off_data={
        "product_name": "   ",
        "nutrients_per_100g": {},
        "categories": [],
    })

    body = client.get("/api/v1/lookup/028400642255").json()
    assert body["product_name"] == "Unknown"


async def test_non_string_usda_description_does_not_override_off(monkeypatch):
    patch_upstreams(
        monkeypatch,
        off_data={
            "product_name": "Good OFF Name",
            "nutrients_per_100g": {},
            "categories": [],
        },
        usda_data={"description": 12345, "nutrients": {}},
    )

    body = client.get("/api/v1/lookup/028400642255").json()
    assert body["product_name"] == "Good OFF Name"


async def test_usda_nutrients_not_a_dict_is_ignored(monkeypatch):
    patch_upstreams(
        monkeypatch,
        off_data=None,
        usda_data={"description": "COLA", "nutrients": ["not", "a", "dict"]},
    )

    resp = client.get("/api/v1/lookup/028400642255")
    assert resp.status_code == 200
    assert resp.json()["calories_kcal"] is None


async def test_blank_usda_ingredients_do_not_replace_missing_off_text(monkeypatch):
    patch_upstreams(
        monkeypatch,
        off_data=None,
        usda_data={"description": "COLA", "nutrients": {}, "ingredients": "   "},
    )

    body = client.get("/api/v1/lookup/028400642255").json()
    assert body["ingredients_text"] is None


# ── property: the response always matches its own schema ──────────────

JUNK = st.one_of(
    st.none(), st.booleans(), st.integers(), st.floats(), st.text(),
    st.lists(st.one_of(st.none(), st.integers(), st.text())),
    st.dictionaries(st.text(), st.text()),
)


@settings(max_examples=150, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    product_name=JUNK, brands=JUNK, image_url=JUNK,
    ingredients_text=JUNK, allergens=JUNK, labels=JUNK, categories=JUNK,
)
async def test_any_off_payload_still_validates_against_the_model(
    monkeypatch, product_name, brands, image_url,
    ingredients_text, allergens, labels, categories,
):
    """Whatever Open Food Facts hands us, the merged product must still be a
    valid CanonicalProduct — not merely one that happens to serialize."""
    patch_upstreams(monkeypatch, off_data={
        "product_name": product_name,
        "brands": brands,
        "image_url": image_url,
        "ingredients_text": ingredients_text,
        "allergens": allergens,
        "labels": labels,
        "categories": categories,
        "nutrients_per_100g": {},
    })

    product = await orchestrator.lookup("028400642255")

    # Re-validating the serialized form is the real check: it proves the
    # values actually conform to the declared types, not just that Pydantic
    # was willing to write them.
    CanonicalProduct.model_validate(jsonable_encoder(product))

    assert isinstance(product.allergens, list)
    assert isinstance(product.labels, list)
    assert isinstance(product.category_hierarchy, list)
    assert isinstance(product.product_name, str)
