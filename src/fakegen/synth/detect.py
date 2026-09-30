"""
detect.py
~~~~~~~~~
Work out what kind of thing each column holds ("semantic type"), from its
header name plus the shape of its values, and decide per value whether it
"conforms" to that type (anything that doesn't is treated as junk and
mirrored as junk in the synthetic output).

Semantic types
--------------
  sin             Canadian Social Insurance Number (9 digits, any separators)
  first_name      given name
  last_name       surname
  full_name       "First Last", "LAST, First M." ...
  email           e-mail address
  phone           phone number
  street_address  "123 Main St Apt 4"
  city            city / town
  province        Canadian province / territory (name or abbreviation)
  postal_code     Canadian postal code (A1A 1A1)
  date            a date or date-time, in any of many common formats
  numeric         a number (amounts, counts, measurements ...)
  category        a small set of repeating values (status, region, codes ...)
  free_text       notes / comments / descriptions
  identifier      an ID-like value (account numbers, case IDs ...)
  unknown         anything else -- regenerated character-by-character
  empty           every sampled value was blank / null
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Optional

SEMANTIC_TYPES = (
    "sin", "first_name", "last_name", "full_name", "email", "phone",
    "street_address", "city", "province", "postal_code", "date", "numeric",
    "category", "free_text", "identifier", "unknown", "empty",
)

# Types whose real values must never appear in the output. Every generated
# value in these columns goes through the leak guard (see guard.py).
SENSITIVE_TYPES = frozenset({
    "sin", "first_name", "last_name", "full_name", "email", "phone",
    "street_address", "city", "postal_code", "free_text", "identifier", "unknown",
})

# Header hints that mean "this is about a person" -- such a column is never
# treated as a `category`, however few distinct values it has.
PERSONAL_HINTS = frozenset({
    "sin", "first_name", "last_name", "full_name", "email", "phone",
    "street_address", "postal_code",
})

PROVINCES = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba",
    "NB": "New Brunswick", "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia", "NT": "Northwest Territories", "NU": "Nunavut",
    "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec",
    "SK": "Saskatchewan", "YT": "Yukon",
}
_PROVINCE_ALIASES = {
    "pq": "QC", "québec": "QC", "que": "QC", "nf": "NL", "nfld": "NL",
    "newfoundland": "NL", "pei": "PE", "p.e.i": "PE", "yukon territory": "YT",
    "sask": "SK", "alta": "AB", "man": "MB", "ont": "ON", "b.c": "BC",
    "nwt": "NT", "yk": "YT",
}
PROVINCE_LOOKUP = {
    **{abbr.lower(): abbr for abbr in PROVINCES},
    **{name.lower(): abbr for abbr, name in PROVINCES.items()},
    **_PROVINCE_ALIASES,
}

# First letter of a postal code is determined by province.
POSTAL_FIRST_LETTERS = {
    "NL": "A", "NS": "B", "PE": "C", "NB": "E", "QC": "GHJ", "ON": "KLMNP",
    "MB": "R", "SK": "S", "AB": "T", "BC": "V", "NT": "X", "NU": "X", "YT": "Y",
}

DATE_FORMATS = [
    "%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y",
    "%d.%m.%Y", "%d/%m/%y", "%m/%d/%y", "%Y%m%d", "%d%b%Y", "%d-%b-%Y",
    "%d-%b-%y", "%d %b %Y", "%b %d, %Y", "%B %d, %Y", "%d %B %Y",
    "%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M",
]
_MONTH_FIRST = {"%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"}

SIN_RE = re.compile(r"\d{3}[ .-]?\d{3}[ .-]?\d{3}")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
POSTAL_RE = re.compile(r"[A-Za-z]\d[A-Za-z][ -]*\d[A-Za-z]\d")
PHONE_STRICT_RE = re.compile(r"(\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]\d{4}")
PHONE_LOOSE_RE = re.compile(r"[\d\s().+-]+")
NAME_RE = re.compile(r"[^\W\d_]+(?:[ '\-.’]+[^\W\d_]+)*\.?")
FULL_NAME_RE = re.compile(r"[^\W\d_]+(?:[ ,'\-.’]+[^\W\d_]+)+\.?")
STREET_STRONG_RE = re.compile(
    r"\d+[A-Za-z]?(?:-\d+)?\s+.*\b(st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|"
    r"cres|crescent|way|lane|ln|court|ct|crt|rue|ch|chemin|pl|place|trail|hwy|highway|"
    r"pkwy|parkway|terr|terrace|cir|circle)\b.*",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(r"([+-]?)(\$?)(\d{1,3}(?:,\d{3})+|\d+)?(?:\.(\d+))?(%?)")
_LETTER_RE = re.compile(r"[^\W\d_]")
_NAME_WORD_RE = re.compile(r"[^\W\d_][^\W\d_'\-’]*\.?")


# ─── header-name hints ────────────────────────────────────────────────────────

def _tokens(name: str) -> list[str]:
    split_camel = re.sub(r"([a-z])([A-Z])", r"\1_\2", str(name))
    return [t for t in re.split(r"[^a-z0-9]+", split_camel.lower()) if t]


_NAME_OWNERS = {
    "full", "client", "person", "customer", "patient", "employee", "contact",
    "member", "applicant", "beneficiary", "recipient", "claimant", "student",
    "owner", "holder", "legal", "display", "spouse", "name",
}
# Headers like "case_worker" or "assigned_officer" name a person even without "name".
_PERSON_WORDS = {
    "worker", "caseworker", "staff", "agent", "officer", "manager", "supervisor", "clerk",
    "author", "employee", "patient", "beneficiary", "applicant", "claimant", "recipient",
    "spouse", "guardian", "counsellor", "counselor", "adjudicator", "assessor", "reviewer",
}
_PERSON_QUALIFIERS = {"assigned", "primary", "case", "full", "name", "responsible", "lead"}


def column_hint(name: str) -> Optional[str]:
    """Best guess at a semantic type from the column header alone (or None)."""
    tokens = _tokens(name)
    t = set(tokens)
    compact = "".join(tokens)
    if t & {"sin", "ssn", "nas", "sinno", "sinnum"} or "socialinsurance" in compact or "socialsecurity" in compact:
        return "sin"
    if "email" in compact or "courriel" in compact:
        return "email"
    if t & {"phone", "tel", "telephone", "mobile", "cell", "fax", "cellphone"} or "phone" in compact:
        return "phone"
    if "postal" in compact or "postcode" in compact or "zip" in t or "zipcode" in compact:
        return "postal_code"
    if t & {"province", "prov", "state", "territory"} or "province" in compact:
        return "province"
    if t & {"city", "town", "municipality", "ville"}:
        return "city"
    if "firstname" in compact or "givenname" in compact or t & {"fname", "first", "given", "prenom", "forename"}:
        return "first_name"
    if "lastname" in compact or "surname" in compact or "familyname" in compact or t & {"lname", "last", "nom", "family"}:
        return "last_name"
    if compact in {"fullname", "clientname", "customername", "patientname", "employeename",
                   "personname", "contactname", "membername", "name"}:
        return "full_name"
    if "name" in t and t <= _NAME_OWNERS:
        return "full_name"
    if t & _PERSON_WORDS and t <= _PERSON_WORDS | _PERSON_QUALIFIERS:
        return "full_name"
    if "address" in compact or "addr" in compact or t & {"street", "adresse", "line1"}:
        return "street_address"
    if t & {"date", "dob", "dt", "dte", "birthdate", "birthday"} or compact.endswith("date") or "birth" in compact:
        return "date"
    if t & {"note", "notes", "comment", "comments", "description", "desc", "remarks", "memo", "narrative"}:
        return "free_text"
    if t & {"id", "no", "num", "number", "nbr", "code", "key", "acct", "account", "ref",
            "uid", "guid", "uuid", "file", "case", "claim", "policy"} or compact.endswith("id"):
        return "identifier"
    return None


# ─── value-level checks ───────────────────────────────────────────────────────

def looks_like_person_name(v: str) -> bool:
    """'Julie Brooks', 'SMITH, John A.' -- 2 to 4 capitalised alphabetic words."""
    words = v.replace(",", " ").split()
    return 2 <= len(words) <= 4 and all(w[0].isupper() and _NAME_WORD_RE.fullmatch(w) for w in words)


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def province_abbr(core: str) -> Optional[str]:
    return PROVINCE_LOOKUP.get(core.strip().rstrip(".").casefold())


def parse_number(core: str) -> Optional[tuple[float, tuple]]:
    """Parse '1,234.50', '$12', '-3', '007', '45%' -> (value, format tuple)."""
    m = NUMBER_RE.fullmatch(core)
    if not m:
        return None
    sign, currency, int_part, decimals, pct = m.groups()
    if int_part is None and decimals is None:
        return None
    int_digits = (int_part or "0").replace(",", "")
    value = float(f"{int_digits}.{decimals}" if decimals else int_digits)
    if sign == "-":
        value = -value
    zero_pad = len(int_part) if int_part and "," not in int_part and len(int_part) > 1 and int_part[0] == "0" else 0
    fmt = (currency, bool(int_part and "," in int_part), len(decimals or ""), zero_pad, pct)
    return value, fmt


def date_formats_for(values: list[str]) -> list[str]:
    """DATE_FORMATS, reordered so month-first formats win if the column is month-first."""
    day_first = month_first = 0
    for v in values:
        m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-]\d{2,4}.*", v)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            day_first += a > 12
            month_first += b > 12
    if month_first > day_first:
        return sorted(DATE_FORMATS, key=lambda f: f not in _MONTH_FIRST)
    return list(DATE_FORMATS)


_DIRECTIVE_PATTERNS = {
    "%Y": r"\d{4}", "%y": r"\d{2}", "%m": r"\d{1,2}", "%d": r"\d{1,2}", "%H": r"\d{1,2}",
    "%M": r"\d{1,2}", "%S": r"\d{1,2}", "%b": r"[^\W\d_]{3,4}", "%B": r"[^\W\d_]{3,9}",
}


def _format_regex(fmt: str) -> re.Pattern:
    parts = re.split(r"(%[A-Za-z])", fmt)
    return re.compile("".join(_DIRECTIVE_PATTERNS.get(p, re.escape(p)) for p in parts))


# strptime is slow, so each format is first checked with a cheap regex.
_FORMAT_REGEXES = {fmt: _format_regex(fmt) for fmt in DATE_FORMATS}


def parse_date(core: str, formats: list[str]) -> Optional[tuple[dt.datetime, str]]:
    if not core or len(core) > 30 or not core[0].isalnum():
        return None
    for fmt in formats:
        if not _FORMAT_REGEXES[fmt].fullmatch(core):
            continue
        try:
            parsed = dt.datetime.strptime(core, fmt)
        except ValueError:
            continue
        if 1800 <= parsed.year <= 2200:
            return parsed, fmt
    return None


def conforms(semantic: str, core: str) -> bool:
    """Does one (whitespace-stripped, non-null) value fit its column's semantic type?"""
    if semantic == "sin":
        return bool(SIN_RE.fullmatch(core))
    if semantic == "email":
        return bool(EMAIL_RE.fullmatch(core))
    if semantic == "postal_code":
        return bool(POSTAL_RE.fullmatch(core))
    if semantic == "phone":
        n_digits = sum(c.isdigit() for c in core)
        return bool(PHONE_LOOSE_RE.fullmatch(core)) and 7 <= n_digits <= 11
    if semantic in ("first_name", "last_name", "city"):
        return bool(NAME_RE.fullmatch(core))
    if semantic == "full_name":
        return bool(FULL_NAME_RE.fullmatch(core))
    if semantic == "street_address":
        return len(core) >= 3 and bool(_LETTER_RE.search(core))
    if semantic == "province":
        return province_abbr(core) is not None
    if semantic == "numeric":
        return parse_number(core) is not None
    # date is checked by parse_date (needs the column's format order);
    # category / free_text / identifier / unknown accept anything.
    return True


# ─── column-level detection ───────────────────────────────────────────────────

def _rate(values: list[str], pred) -> float:
    return sum(1 for v in values if pred(v)) / len(values) if values else 0.0


def detect_semantic(
    name: str,
    values: list[str],
    n_distinct: int,
    n_values: int,
    category_threshold: int = 50,
) -> tuple[str, str]:
    """
    Decide a column's semantic type.

    `values` is a subset (up to a few thousand) of the column's stripped,
    non-null values; `n_distinct` / `n_values` describe the whole profiled
    sample. Returns (semantic, human-readable reason).
    """
    if not values:
        return "empty", "no non-blank values"

    hint = column_hint(name)

    def strong(sem: str, rate: float) -> bool:
        return rate >= 0.8 or (hint == sem and rate >= 0.5)

    sin_rate = _rate(values, SIN_RE.fullmatch)
    if hint == "sin" and sin_rate >= 0.5:
        return "sin", f"header suggests SIN; {sin_rate:.0%} of values are 9-digit numbers"
    if sin_rate >= 0.8:
        digit_vals = [re.sub(r"\D", "", v) for v in values if SIN_RE.fullmatch(v)]
        if _rate(digit_vals, luhn_ok) >= 0.8:
            return "sin", f"{sin_rate:.0%} of values are 9-digit numbers passing the SIN checksum"

    rate = _rate(values, EMAIL_RE.fullmatch)
    if strong("email", rate):
        return "email", f"{rate:.0%} of values look like e-mail addresses"

    rate = _rate(values, POSTAL_RE.fullmatch)
    if strong("postal_code", rate):
        return "postal_code", f"{rate:.0%} of values look like Canadian postal codes"

    if hint == "phone":
        rate = _rate(values, lambda v: conforms("phone", v))
        if rate >= 0.5:
            return "phone", f"header suggests phone; {rate:.0%} of values look like phone numbers"
    rate = _rate(values, PHONE_STRICT_RE.fullmatch)
    if rate >= 0.8:
        return "phone", f"{rate:.0%} of values look like phone numbers"

    formats = date_formats_for(values)
    rate = _rate(values, lambda v: parse_date(v, formats) is not None)
    if strong("date", rate):
        return "date", f"{rate:.0%} of values parse as dates"

    rate = _rate(values, lambda v: province_abbr(v) is not None)
    if strong("province", rate):
        return "province", f"{rate:.0%} of values are Canadian provinces/territories"

    for sem in ("first_name", "last_name", "city"):
        if hint == sem:
            rate = _rate(values, NAME_RE.fullmatch)
            if rate >= 0.5:
                return sem, f"header suggests {sem.replace('_', ' ')}; {rate:.0%} of values are alphabetic"
    if hint == "full_name":
        rate = _rate(values, FULL_NAME_RE.fullmatch)
        if rate >= 0.5:
            return "full_name", f"header suggests a person's name; {rate:.0%} of values are multi-word names"

    street_rate = _rate(values, STREET_STRONG_RE.fullmatch)
    if (hint == "street_address" and _rate(values, lambda v: conforms("street_address", v)) >= 0.5) or street_rate >= 0.8:
        return "street_address", "header and/or values look like street addresses"

    if n_distinct <= category_threshold and n_distinct <= 0.5 * n_values and hint not in PERSONAL_HINTS:
        return "category", f"{n_distinct} distinct values repeated across {n_values} rows"

    rate = _rate(values, looks_like_person_name)
    if rate >= 0.8 and n_distinct >= 0.3 * n_values:
        return "full_name", f"{rate:.0%} of values look like people's names"

    num_rate = _rate(values, lambda v: parse_number(v) is not None)
    if num_rate >= 0.8:
        leading_zero = _rate(values, lambda v: len(v) > 1 and v[0] == "0" and v[1].isdigit())
        if hint == "identifier" or leading_zero >= 0.2:
            return "identifier", "numeric-looking ID (header name or leading zeros)"
        return "numeric", f"{num_rate:.0%} of values are numbers"

    avg_len = sum(len(v) for v in values) / len(values)
    avg_words = sum(len(v.split()) for v in values) / len(values)
    if avg_words >= 4 or avg_len >= 40 or (hint == "free_text" and avg_len >= 15):
        return "free_text", f"long values (avg {avg_len:.0f} chars, {avg_words:.1f} words)"

    if hint == "identifier" or n_distinct >= 0.9 * n_values:
        return "identifier", "mostly-unique values -- regenerated with the same character pattern"

    return "unknown", "no confident match -- regenerated with the same character pattern"
