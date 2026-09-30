import random

from fakegen.synth.detect import column_hint, detect_semantic, luhn_ok, parse_number
from fakegen.synth.guard import LeakGuard
from fakegen.synth.render import fill_mask, fill_mask_with, format_number, mask_of


def _detect(name, values, **kw):
    return detect_semantic(name, values, len(set(values)), len(values), **kw)[0]


def test_header_hints():
    assert column_hint("SIN_NO") == "sin"
    assert column_hint("Social Insurance Number") == "sin"
    assert column_hint("business_type") is None  # 'sin' inside a word is not a hint
    assert column_hint("FirstName") == "first_name"
    assert column_hint("client_name") == "full_name"
    assert column_hint("case_worker") == "full_name"
    assert column_hint("Postal Code") == "postal_code"
    assert column_hint("email_address") == "email"


def test_detects_sin_in_mixed_formats():
    values = ["046 454 286", "046-454-286", "046454286", "130692544"] * 10
    assert _detect("SIN", values) == "sin"
    # Without a header hint it still needs the checksum to pass.
    assert _detect("col_7", values) == "sin"


def test_detects_common_types_from_values_alone():
    assert _detect("c1", ["a.b@x.com", "c@y.org", "ZED@Q.CA"] * 5) == "email"
    assert _detect("c2", ["K1A 0B1", "m5v3l9", "V6B 1A1"] * 5) == "postal_code"
    assert _detect("c3", ["2020-01-31", "31/01/2021", "15JAN2019"] * 5 + ["2021-02-03"]) == "date"
    assert _detect("c4", ["ON", "Quebec", "b.c."] * 5) == "province"
    assert _detect("c5", [str(i * 1.5) for i in range(100)]) == "numeric"
    assert _detect("status", ["Active", "Inactive", "Pending"] * 20) == "category"
    assert _detect("c6", [f"Name{i} Person{i}" for i in range(20)]) != "category"


def test_personal_header_never_becomes_category():
    # Only 2 distinct names repeated many times -- still names, not a category.
    assert _detect("first_name", ["Anne", "Bob"] * 30) == "first_name"


def test_numeric_ids_with_leading_zeros_are_identifiers():
    assert _detect("x", [f"{i:08d}" for i in range(100)]) == "identifier"


def test_luhn_and_numbers():
    assert luhn_ok("046454286")
    assert not luhn_ok("046454287")
    value, fmt = parse_number("$1,234.50")
    assert value == 1234.5
    assert format_number(98765.4321, fmt) == "$98,765.43"
    assert format_number(7, parse_number("007")[1]) == "007"


def test_masks_keep_shape_not_characters():
    rng = random.Random(0)
    assert mask_of(" K1a-0B1 ") == " A9a-9A9 "
    filled = fill_mask("AA-9999", rng)
    assert filled[:2].isupper() and filled[2] == "-" and filled[3:].isdigit()
    assert fill_mask_with("999 999 999", "123456789", rng) == "123 456 789"
    assert fill_mask_with("a9a 9a9", "K1A0B1", rng) == "k1a 0b1"


def test_leak_guard_matches_reformatted_values():
    g = LeakGuard()
    g.add_real("123 456 789")
    g.add_real("Smith, John")
    assert g.is_leak("123-456-789")
    g.add_real("987654321")
    assert g.is_leak("987 654-321")
    assert g.is_leak("  JOHN smith ")
    assert not g.is_leak("123 456 780")
    assert not g.is_leak("QC")  # too short to guard
