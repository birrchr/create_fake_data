"""
profile.py
~~~~~~~~~~
Learn the *shape* of every column in a source file -- never its sensitive
values -- so a synthetic twin can be generated from it.

For each column a ColumnProfile records:

  - how often each kind of blank appears ("", "  ", "N/A", "NULL", ...)
  - the semantic type (see detect.py) and why it was chosen
  - how often values DON'T fit that type ("junk"), and the masks of that junk
  - for values that DO fit: their formats/masks/casing/whitespace, and for
    numbers and dates, the observed distribution

What a profile keeps from the real data:
  - masks (digits -> 9, letters -> A/a): shapes only, no real characters
  - blank tokens, from a fixed whitelist
  - numbers and dates (to learn their distribution; outputs are re-sampled)
  - category values and e-mail domains that appear at least `k` times
    (k-anonymity); rarer ones are reduced to masks
  - characters that are identical across >= k values of an ID mask (e.g. a
    shared "CL-" prefix)
Everything else is discarded as soon as the profile is built.
"""

from __future__ import annotations

import datetime as dt
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from itertools import combinations
from typing import Optional

from .detect import (
    detect_semantic, conforms, date_formats_for, luhn_ok, parse_date, parse_number, province_abbr,
)
from .render import case_style, mask_of, split_ws, text_style

EPOCH = dt.datetime(1970, 1, 1)
NULL_TOKENS = frozenset({
    "", "N/A", "n/a", "NA", "na", "N.A.", "NULL", "null", "Null", "None", "none", "NONE",
    "-", "--", "---", ".", "?", "??", "#N/A", "nan", "NaN", "NAN", "(blank)", "(null)",
    "<null>", "UNKNOWN", "Unknown", "unknown", "nil", "NIL",
})
DETECTION_SUBSET = 2000
_NAME_WORD = re.compile(r"[^\W\d_]+(?:['\-’][^\W\d_]+)*\.?")


def is_null_token(v: Optional[str]) -> bool:
    return v is None or v.strip() in NULL_TOKENS


@dataclass
class ColumnProfile:
    index: int
    name: str
    semantic: str
    reason: str
    n: int = 0
    null_tokens: Counter = field(default_factory=Counter)
    junk_masks: Counter = field(default_factory=Counter)
    n_junk: int = 0
    n_conforming: int = 0
    styles: Counter = field(default_factory=Counter)      # (case, lead, trail[, extra])
    masks: Counter = field(default_factory=Counter)       # masks of conforming values
    fixed_chars: dict = field(default_factory=dict)       # mask -> {position: char}
    formats: Counter = field(default_factory=Counter)     # date / number formats (+ whitespace)
    numbers: list = field(default_factory=list)           # numeric values / date seconds
    categories: Counter = field(default_factory=Counter)  # values seen >= k times
    rare_masks: Counter = field(default_factory=Counter)  # masks of category values seen < k times
    luhn_valid: int = 0
    domains: Counter = field(default_factory=Counter)
    provinces: Counter = field(default_factory=Counter)
    postal_letters: Counter = field(default_factory=Counter)
    name_templates: Counter = field(default_factory=Counter)
    lengths: list = field(default_factory=list)

    @property
    def null_rate(self) -> float:
        return sum(self.null_tokens.values()) / self.n if self.n else 1.0

    @property
    def junk_rate(self) -> float:
        return self.n_junk / self.n if self.n else 0.0

    @property
    def n_shapes(self) -> int:
        """How many distinct formats/masks/styles were seen (a rough 'messiness' score)."""
        return len(self.masks) + len(self.formats) + len(self.styles) + len(self.junk_masks) + len(self.rare_masks)


def build_profiles(
    columns: list[str],
    rows: list[list[Optional[str]]],
    k: int = 5,
    category_threshold: int = 50,
    overrides: Optional[dict[str, str]] = None,
) -> tuple[list[ColumnProfile], list[list[int]]]:
    """
    Profile every column of `rows`. Returns (profiles, date_groups) where each
    date group is a list of date-column indexes whose values are always in
    that order within a row in the source (e.g. [birth_date, service_date]).
    """
    overrides = overrides or {}
    profiles: list[ColumnProfile] = []
    parsed_dates: dict[int, list] = {}
    for i, name in enumerate(columns):
        values = [r[i] for r in rows]
        profile, dates = _profile_column(i, name, values, k, category_threshold, overrides.get(name))
        profiles.append(profile)
        if dates is not None:
            parsed_dates[i] = dates
    return profiles, _date_groups(profiles, parsed_dates)


def _profile_column(index, name, values, k, category_threshold, override):
    nulls: Counter = Counter()
    non_null: list[str] = []
    for v in values:
        if is_null_token(v):
            nulls[None if v is None else v[:20]] += 1
        else:
            non_null.append(v)

    cores = [v.strip() for v in non_null]
    step = max(1, len(cores) // DETECTION_SUBSET)
    subset = cores[::step][:DETECTION_SUBSET]
    if override:
        semantic, reason = override, "set in overrides file"
    else:
        semantic, reason = detect_semantic(name, subset, len(set(non_null)), len(non_null), category_threshold)

    p = ColumnProfile(index=index, name=name, semantic=semantic, reason=reason, n=len(values), null_tokens=nulls)
    handler = _HANDLERS.get(semantic, _handle_text)
    date_formats = date_formats_for(subset) if semantic == "date" else None
    parsed = [None] * len(values) if semantic == "date" else None
    templates: dict[str, list] = {}

    for row_i, v in enumerate(values):
        if is_null_token(v):
            continue
        lead, core, trail = split_ws(v)
        if semantic == "date":
            ok = _handle_date(p, v, lead, core, trail, date_formats, parsed, row_i)
        else:
            ok = handler(p, v, lead, core, trail, templates)
        if ok:
            p.n_conforming += 1
        else:
            p.n_junk += 1
            p.junk_masks[mask_of(v)] += 1

    _finalize(p, k, templates)
    return p, parsed


# ─── per-semantic value handlers (return True if the value conforms) ─────────

def _handle_masked(p, v, lead, core, trail, templates):
    if not conforms(p.semantic, core):
        return False
    m = mask_of(v)
    p.masks[m] += 1
    if p.semantic == "sin":
        p.luhn_valid += luhn_ok(re.sub(r"\D", "", core))
    elif p.semantic == "postal_code":
        p.postal_letters[core[0].upper()] += 1
    elif p.semantic in ("identifier", "unknown"):
        tpl = templates.get(m)
        if tpl is None:
            templates[m] = list(v)
        else:
            for pos, ch in enumerate(v):
                if tpl[pos] is not None and tpl[pos] != ch:
                    tpl[pos] = None
    return True


def _handle_text(p, v, lead, core, trail, templates):
    if not conforms(p.semantic, core):
        return False
    p.styles[text_style(v)] += 1
    if p.semantic == "email":
        p.domains[core.rsplit("@", 1)[1].casefold()] += 1
    elif p.semantic == "free_text":
        p.lengths.append(len(core))
    return True


def _handle_full_name(p, v, lead, core, trail, templates):
    template = _name_template(core)
    if template is None:
        return False
    p.styles[text_style(v)] += 1
    p.name_templates[template] += 1
    return True


def _handle_province(p, v, lead, core, trail, templates):
    abbr = province_abbr(core)
    if abbr is None:
        return False
    form = "abbr" if len(core.rstrip(".")) <= 4 else "name"
    p.styles[(case_style(core), lead, trail, form)] += 1
    p.provinces[abbr] += 1
    return True


def _handle_numeric(p, v, lead, core, trail, templates):
    parsed = parse_number(core)
    if parsed is None:
        return False
    value, fmt = parsed
    p.numbers.append(value)
    p.formats[(fmt, lead, trail)] += 1
    return True


def _handle_category(p, v, lead, core, trail, templates):
    p.categories[v] += 1
    return True


def _handle_date(p, v, lead, core, trail, formats, parsed, row_i):
    result = parse_date(core, formats)
    if result is None:
        return False
    when, fmt = result
    seconds = (when - EPOCH).total_seconds()
    p.numbers.append(seconds)
    p.formats[(fmt, case_style(core), lead, trail)] += 1
    parsed[row_i] = seconds
    return True


_HANDLERS = {
    "sin": _handle_masked, "phone": _handle_masked, "postal_code": _handle_masked,
    "identifier": _handle_masked, "unknown": _handle_masked,
    "full_name": _handle_full_name, "province": _handle_province,
    "numeric": _handle_numeric, "category": _handle_category,
}


def _name_template(core: str) -> Optional[str]:
    words = _NAME_WORD.findall(core)
    if len(words) < 2 or len(words) > 5:
        return None
    if "," in core:
        first, middle = words[1], words[2:]
        head = "{last}, {first}"
    else:
        first, middle = words[0], words[1:-1]
        head = None
    mid = ""
    if middle:
        m = middle[0]
        if len(m.rstrip(".")) == 1:
            mid = "{mi}" + ("." if m.endswith(".") else "")
        else:
            mid = "{middle}"
    if head:
        return head + (f" {mid}" if mid else "")
    return "{first}" + (f" {mid}" if mid else "") + " {last}"


# ─── finishing touches: apply k-anonymity, drop anything too specific ────────

def _finalize(p: ColumnProfile, k: int, templates: dict) -> None:
    if p.semantic == "category":
        common = Counter({v: c for v, c in p.categories.items() if c >= k})
        for v, c in p.categories.items():
            if c < k:
                p.rare_masks[mask_of(v)] += c
        p.categories = common
    if p.domains:
        p.domains = Counter({d: c for d, c in p.domains.items() if c >= k})
    for m, tpl in templates.items():
        if p.masks[m] >= k:
            fixed = {i: ch for i, ch in enumerate(tpl) if ch is not None and ch.isalnum()}
            if fixed:
                p.fixed_chars[m] = fixed
    p.numbers.sort()


def _date_groups(profiles: list[ColumnProfile], parsed: dict[int, list]) -> list[list[int]]:
    """Find date columns that are always ordered within a row (a <= b) and group them."""
    parent = {i: i for i in parsed}

    def find(i):
        while parent[i] != i:
            i = parent[i]
        return i

    for a, b in combinations(parsed, 2):
        pairs = [(x, y) for x, y in zip(parsed[a], parsed[b]) if x is not None and y is not None]
        if len(pairs) < 10:
            continue
        le = sum(x <= y for x, y in pairs) / len(pairs)
        ge = sum(x >= y for x, y in pairs) / len(pairs)
        if (le >= 0.99 or ge >= 0.99) and not (le == 1 and ge == 1):
            parent[find(a)] = find(b)

    groups: dict[int, list[int]] = {}
    for i in parsed:
        groups.setdefault(find(i), []).append(i)
    by_index = {p.index: p for p in profiles}
    return [
        sorted(g, key=lambda i: statistics.median(by_index[i].numbers))
        for g in groups.values() if len(g) > 1 and all(by_index[i].numbers for i in g)
    ]
