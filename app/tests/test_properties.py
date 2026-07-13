"""
Property-based tests.

The example-based tests above pin the cases we thought of. These assert
invariants that must hold for *every* input, and let Hypothesis go looking for
the ones we didn't think of — which is exactly how the ">100" nutrient crash
and the "unknown" version comparison would have been caught.

Copyright (c) 2026 Michael McGarrah
Licensed under MIT License
"""
import json
import sys
from pathlib import Path

from fastapi.encoders import jsonable_encoder
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.core.orchestrator import _num, _nv, _usda_nutrient
from app.core.usda_fdc import normalize_gtin

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import import_gpc_xml as importer  # noqa: E402


# Anything at all: strings, numbers, containers, None, nested junk.
ANYTHING = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats()
    | st.text()
    | st.binary(),
    lambda children: st.lists(children) | st.dictionaries(st.text(), children),
    max_leaves=5,
)


# ── normalize_gtin ────────────────────────────────────────────────────

@given(ANYTHING)
def test_normalize_gtin_never_raises(value):
    """It is the last line of defence in front of the USDA matcher; if it can
    raise, a malformed upstream record takes the request down with it."""
    normalize_gtin(value)


@given(ANYTHING)
def test_normalize_gtin_returns_empty_or_14_digits(value):
    result = normalize_gtin(value)
    assert result == "" or (len(result) == 14 and result.isdigit())


@given(st.text(alphabet="0123456789", min_size=1, max_size=14))
def test_normalize_gtin_is_idempotent(digits):
    once = normalize_gtin(digits)
    assert normalize_gtin(once) == once


@given(st.text(alphabet="0123456789", min_size=1, max_size=14))
def test_normalize_gtin_preserves_the_digit_sequence(digits):
    """Padding may only add leading zeros — never reorder or drop digits."""
    result = normalize_gtin(digits)
    assert result.endswith(digits)
    assert result[: 14 - len(digits)] == "0" * (14 - len(digits))


@given(st.text(alphabet="0123456789", min_size=1, max_size=14), st.integers(0, 6))
def test_zero_padding_variants_of_a_gtin_are_equivalent(digits, extra_zeros):
    """The rule the USDA matcher depends on: the same barcode written with
    different leading-zero padding must compare equal."""
    padded = "0" * extra_zeros + digits
    assume(len(padded) <= 14)
    assert normalize_gtin(padded) == normalize_gtin(digits)


@given(st.text(alphabet="0123456789", min_size=15, max_size=40))
def test_overlong_digit_strings_are_rejected(digits):
    """Longer than a GTIN-14 is not a barcode — it must not silently truncate."""
    assert normalize_gtin(digits) == ""


# ── numeric coercion (the ">100" crash) ───────────────────────────────

@given(ANYTHING)
def test_num_never_raises(value):
    _num(value)


@given(ANYTHING)
def test_nv_never_raises(value):
    _nv(value)


@given(ANYTHING)
def test_num_returns_a_float_or_none(value):
    result = _num(value)
    assert result is None or isinstance(result, float)


@given(ANYTHING)
def test_nv_result_is_always_strict_json(value):
    """Whatever comes back must survive *strict* JSON encoding.

    NaN and Infinity are floats, so they pass float() silently — but JSON has
    no literal for them. Python emits the bare tokens NaN/Infinity, which Go,
    Jackson, and JSON.parse all reject, poisoning the entire response rather
    than the single field. This property is what caught that.
    """
    nv = _nv(value)
    if nv is not None:
        encoded = json.dumps(jsonable_encoder(nv), allow_nan=False)
        assert json.loads(encoded)["value"] == nv.value


@given(ANYTHING)
def test_num_result_is_always_strict_json(value):
    number = _num(value)
    if number is not None:
        json.dumps(number, allow_nan=False)


@given(st.floats(allow_nan=False, allow_infinity=False))
def test_finite_numbers_always_survive(value):
    assert _num(value) == value
    assert _nv(value).value == value


@given(st.sampled_from([float("nan"), float("inf"), float("-inf"),
                        "nan", "NaN", "inf", "Infinity", "-Infinity"]))
def test_non_finite_values_are_dropped(value):
    """float() accepts every one of these — the guard must not."""
    assert _num(value) is None
    assert _nv(value) is None


@given(st.dictionaries(st.text(), ANYTHING), st.text())
def test_usda_nutrient_never_raises(nutrients, name):
    _usda_nutrient(nutrients, name)


# ── version comparison (the frozen auto-update) ───────────────────────

VERSIONS = st.text(alphabet="0123456789", min_size=8, max_size=8)


@given(st.one_of(st.none(), ANYTHING, VERSIONS), st.one_of(st.none(), ANYTHING, VERSIONS))
def test_is_remote_newer_never_raises(remote, stored):
    assume(remote is None or isinstance(remote, str))
    assume(stored is None or isinstance(stored, str))
    importer.is_remote_newer(remote, stored)


@given(VERSIONS)
def test_a_version_never_supersedes_itself(version):
    assert importer.is_remote_newer(version, version) is False
    assert importer.is_remote_newer("v" + version, version) is False


@given(VERSIONS, VERSIONS)
def test_newer_is_antisymmetric(a, b):
    """Both directions can't be an upgrade — that would flip-flop the importer
    into re-downloading 27 MB on every single boot."""
    assume(a != b)
    assert not (importer.is_remote_newer(a, b) and importer.is_remote_newer(b, a))


@given(VERSIONS, st.text())
def test_an_unusable_stored_version_always_defers_to_a_valid_remote(remote, junk):
    """The bug that froze the auto-update: 'unknown' must never outrank a real
    date, no matter what the junk happens to be."""
    assume(not junk.lstrip("v").isdigit())
    assert importer.is_remote_newer(remote, junk) is True


@given(st.text(), VERSIONS)
def test_an_unusable_remote_version_never_triggers_a_rebuild(junk, stored):
    assume(not junk.lstrip("v").isdigit())
    assert importer.is_remote_newer(junk, stored) is False


# ── date extraction ───────────────────────────────────────────────────

@given(st.text(), st.text())
@settings(max_examples=200)
def test_extract_version_never_raises(path, date):
    importer.extract_version_from_path(path, date)


@given(
    st.integers(min_value=1, max_value=28),
    st.integers(min_value=1, max_value=12),
    st.integers(min_value=2000, max_value=2099),
)
def test_day_first_dates_round_trip(day, month, year):
    """GS1 publishes D/M/YYYY; the parsed version must be the same calendar day."""
    result = importer.extract_version_from_path("cached.xml", f"{day}/{month}/{year}")
    assert result == f"{year:04d}{month:02d}{day:02d}"
