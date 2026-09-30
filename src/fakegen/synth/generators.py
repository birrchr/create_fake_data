"""
generators.py
~~~~~~~~~~~~~
Turn column profiles back into rows of synthetic data.

Each row starts as a "record" -- one fake person (first/middle/last name,
e-mail user name), one fake Canadian address (street, city, province and a
postal code that matches that province) and one value per date column (put
in the same order the source keeps them in). Each column then renders its
cell from that record in one of the formats seen in the source, or produces
a blank / junk value at the rate those appeared in the source.
"""

from __future__ import annotations

import datetime as dt
import random
import re
import string
import unicodedata
from collections import Counter
from itertools import accumulate
from typing import Callable, Optional

from faker import Faker

from .detect import POSTAL_FIRST_LETTERS, PROVINCES, SENSITIVE_TYPES
from .guard import LeakGuard
from .profile import EPOCH, ColumnProfile
from .render import (
    fill_mask, fill_mask_with, format_number, mask_of, pronounceable, render_text,
)

_POSTAL_LETTERS = "ABCEGHJKLMNPRSTVWXYZ"
_NAME_SEMANTICS = {"first_name", "last_name", "full_name", "email"}
_ADDRESS_SEMANTICS = {"street_address", "city", "province", "postal_code"}
# Values of these types come from the row record, which already went through
# the leak guard; rendering them only changes case/whitespace/punctuation,
# which the guard ignores anyway, so they don't need checking a second time.
_GUARDED_IN_RECORD = {"first_name", "last_name", "city", "street_address", "postal_code"}
# Of those, the ones whose value is a pool entry (see RecordFactory.CHECKED_POOLS).
_POOL_RENDERED = {"first_name", "last_name", "city"}


class Choice:
    """Weighted random pick from a Counter."""

    def __init__(self, counter):
        self.items = list(counter.keys())
        self.cum = list(accumulate(counter.values()))

    def __bool__(self) -> bool:
        return bool(self.items)

    def pick(self, rng: random.Random):
        if len(self.items) == 1:
            return self.items[0]
        return rng.choices(self.items, cum_weights=self.cum)[0]


class Sampler:
    """Sample numbers that follow an observed distribution without copying observed values."""

    def __init__(self, sorted_values: list[float], bins: int = 20):
        n = len(sorted_values)
        if n == 0:
            self.edges = [0.0]
        elif n == 1:
            self.edges = [sorted_values[0]]
        else:
            b = min(bins, n - 1)
            self.edges = [sorted_values[round(i * (n - 1) / b)] for i in range(b + 1)]

    def sample(self, rng: random.Random) -> float:
        if len(self.edges) == 1:
            return self.edges[0]
        i = rng.randrange(len(self.edges) - 1)
        return rng.uniform(self.edges[i], self.edges[i + 1])


class RecordFactory:
    """Builds the per-row fake person / address / dates that columns draw from."""

    def __init__(self, profiles: list[ColumnProfile], date_groups: list[list[int]],
                 fake: Faker, rng: random.Random, guard: LeakGuard, pool_size: int = 3000):
        self.fake, self.rng, self.guard = fake, rng, guard
        self.pool_size = pool_size
        semantics = {p.semantic for p in profiles}
        self.need_names = bool(semantics & _NAME_SEMANTICS)
        self.need_address = bool(semantics & _ADDRESS_SEMANTICS)
        provinces, letters = _merged(profiles, "provinces"), _merged(profiles, "postal_letters")
        self.province_choice = Choice(provinces)
        self.has_province_column = "province" in semantics
        self.postal_letter_choice = Choice({l: c for l, c in letters.items() if l in _POSTAL_LETTERS})
        self.date_samplers = {p.index: Sampler(p.numbers) for p in profiles if p.semantic == "date" and p.numbers}
        self.date_groups = date_groups
        self._pools: dict[str, list[str]] = {}

    # --- pools --------------------------------------------------------------
    # Calling Faker for every cell is slow, so each kind of value is drawn
    # from Faker a few thousand times up front (keeping Faker's frequencies)
    # with any value that matches a real one thrown out before it's ever used.

    def _pool(self, kind: str, label: str, make: Callable[[], str], size: int) -> list[str]:
        pool = self._pools.get(kind)
        if pool is None:
            pool, leaks = [], 0
            for _ in range(size):
                v = make()
                if self.guard.is_leak(v):
                    leaks += 1
                else:
                    pool.append(v)
            if leaks:
                self.guard.record(label, collisions=leaks)
            if not pool:  # every Faker value was a real one -- invent some
                pool = [pronounceable(self.rng, self.rng.randint(4, 8)) for _ in range(500)]
            self._pools[kind] = pool
        return pool

    def _guarded(self, label: str, make: Callable[[], str]) -> str:
        return self.guard.check(label, make(), make, lambda v: pronounceable(self.rng, len(str(v)) or 6))

    # Pools whose values reach the output unchanged (apart from case and
    # whitespace), so checking the pool is as good as checking every cell.
    CHECKED_POOLS = {"first": "(row names)", "last": "(row names)", "city": "(row address)"}

    def warm_pools(self) -> dict[str, list[str]]:
        """Build every pool this file needs now, so large-file mode can check them in one scan."""
        if self.need_names:
            self.new_names()
        if self.need_address:
            self.new_city()
            self.new_street()
        return {kind: self._pools[kind] for kind in self.CHECKED_POOLS if kind in self._pools}

    def drop_from_pool(self, kind: str, indexes: set[int]) -> None:
        """Remove pool entries found to match real values (large-file mode)."""
        if not indexes:
            return
        self.guard.record(self.CHECKED_POOLS[kind], collisions=len(indexes))
        kept = [v for i, v in enumerate(self._pools[kind]) if i not in indexes]
        self._pools[kind] = kept or [pronounceable(self.rng, self.rng.randint(4, 8)) for _ in range(500)]

    # --- individual pieces (also used when a column needs a fresh value) ----

    def new_names(self) -> tuple[str, str, str]:
        firsts = self._pool("first", "(row names)", self.fake.first_name, self.pool_size)
        lasts = self._pool("last", "(row names)", self.fake.last_name, self.pool_size)
        first, middle, last = self.rng.choice(firsts), self.rng.choice(firsts), self.rng.choice(lasts)
        for _ in range(self.guard.retries):
            if not (self.guard.is_leak(f"{first} {last}") or self.guard.is_leak(f"{first} {middle} {last}")):
                break
            self.guard.record("(row names)", collisions=1)
            last = self.rng.choice(lasts)
        return first, middle, last

    def new_email_local(self, first: Optional[str] = None, last: Optional[str] = None) -> str:
        if first is None:
            first, _, last = self.new_names()
        f, l = _ascii_word(first) or "x", _ascii_word(last) or "y"
        pattern = self.rng.randrange(5)
        if pattern == 0:
            return f"{f}.{l}"
        if pattern == 1:
            return f"{f[0]}{l}"
        if pattern == 2:
            return f"{f}{l}{self.rng.randint(1, 99)}"
        if pattern == 3:
            return f"{l}.{f}"
        return f"{f}_{l}"

    def new_province(self) -> str:
        if self.province_choice:
            return self.province_choice.pick(self.rng)
        return self.rng.choice(list(PROVINCES))

    def new_postal(self, province: Optional[str] = None) -> str:
        def make():
            if province and self.has_province_column:
                first = self.rng.choice(POSTAL_FIRST_LETTERS[province])
            elif self.postal_letter_choice:
                first = self.postal_letter_choice.pick(self.rng)
            else:
                first = self.rng.choice(POSTAL_FIRST_LETTERS[province or self.new_province()])
            r = self.rng
            return (first + r.choice(string.digits) + r.choice(_POSTAL_LETTERS)
                    + r.choice(string.digits) + r.choice(_POSTAL_LETTERS) + r.choice(string.digits))
        return self.guard.check("(row address)", make(), make, lambda v: make())

    def new_city(self) -> str:
        return self.rng.choice(self._pool("city", "(row address)", self.fake.city, self.pool_size))

    def new_street(self) -> str:
        streets = self._pool("street", "(row address)", self.fake.street_name, self.pool_size)

        def make():
            number = str(self.rng.randint(1, 10 ** self.rng.randint(2, 4) - 1))
            unit = f" {self.rng.choice(('Apt.', 'Suite', 'Unit'))} {self.rng.randint(1, 999)}" if self.rng.random() < 0.3 else ""
            return f"{number} {self.rng.choice(streets)}{unit}"
        return self._guarded("(row address)", make)

    def new_sin(self) -> str:
        """A random 9-digit number with a valid SIN checksum (Luhn)."""
        digits = [self.rng.choice("1234567")] + [self.rng.choice(string.digits) for _ in range(7)]
        total = 0
        for i, ch in enumerate(digits):
            d = int(ch) * (2 if i % 2 else 1)
            total += d - 9 if d > 9 else d
        return "".join(digits) + str((10 - total % 10) % 10)

    def new_email_domain(self) -> str:
        return self.rng.choice(self._pool("domain", "(row names)", self.fake.free_email_domain, 50))

    # --- a full row record --------------------------------------------------

    def make(self) -> dict:
        rec: dict = {}
        if self.need_names:
            rec["first"], rec["middle"], rec["last"] = self.new_names()
            rec["email_local"] = self.new_email_local(rec["first"], rec["last"])
        if self.need_address:
            rec["province"] = self.new_province()
            rec["postal"] = self.new_postal(rec["province"])
            rec["city"] = self.new_city()
            rec["street"] = self.new_street()
        if self.date_samplers:
            dates = {i: s.sample(self.rng) for i, s in self.date_samplers.items()}
            for group in self.date_groups:
                ordered = sorted(dates[i] for i in group)
                for i, v in zip(group, ordered):
                    dates[i] = v
            rec["dates"] = dates
        return rec


class ColumnGenerator:
    """Produces one column's cells, mirroring the source's blanks, junk and formats."""

    def __init__(self, p: ColumnProfile, factory: RecordFactory, fake: Faker,
                 rng: random.Random, guard: LeakGuard):
        self.p, self.factory, self.fake, self.rng, self.guard = p, factory, fake, rng, guard
        self.null_rate = p.null_rate
        self.junk_rate = p.junk_rate if p.n_conforming else 1.0 - p.null_rate
        self.nulls = Choice(p.null_tokens)
        self.junk = Choice(p.junk_masks)
        self.styles = Choice(p.styles)
        self.masks = Choice(p.masks)
        self.formats = Choice(p.formats)
        self.templates = Choice(p.name_templates)
        self.domains = Choice(p.domains)
        self.sensitive = p.semantic in SENSITIVE_TYPES
        self.check_conforming = self.sensitive and p.semantic not in _GUARDED_IN_RECORD
        self.pool_rendered = p.semantic in _POOL_RENDERED and bool(p.n_conforming)
        self.sampler = Sampler(p.numbers)
        self.luhn_rate = p.luhn_valid / sum(p.masks.values()) if p.semantic == "sin" and p.masks else 1.0
        if p.semantic == "category":
            options = dict(p.categories)
            options.update({_RareMask(m): c for m, c in p.rare_masks.items()})
            self.categories = Choice(options)

    def cell(self, rec: dict):
        r = self.rng.random()
        if r < self.null_rate:
            return self.nulls.pick(self.rng) if self.nulls else None
        if r < self.null_rate + self.junk_rate:
            make = lambda: fill_mask(self.junk.pick(self.rng), self.rng)
            value = make()
            return self._guard(value, make) if self.sensitive else value
        value = self._conforming(rec)
        return self._guard(value, lambda: self._conforming(None)) if self.check_conforming else value

    def pool_replacement(self) -> Optional[str]:
        """
        A replacement that needs no further checking because it comes straight
        from an already-checked pool (first/last names, cities), or None.
        """
        if self.pool_rendered:
            return self._conforming(None)
        return None

    def fresh(self):
        """A new non-blank value for this column, replacing one that matched a real value."""
        p = self.p
        if p.n_conforming and (not self.junk or self.rng.random() * (p.n_conforming + p.n_junk) < p.n_conforming):
            return self._conforming(None)
        return fill_mask(self.junk.pick(self.rng), self.rng)

    def _guard(self, value, regenerate):
        return self.guard.check(self.p.name, value, regenerate,
                                lambda v: fill_mask(mask_of(str(v)), self.rng))

    def _conforming(self, rec: Optional[dict]):
        """A well-formed value; `rec` is None when a fresh (non-row) value is needed."""
        p, rng, f = self.p, self.rng, self.factory
        sem = p.semantic

        if sem == "sin":
            digits = f.new_sin()
            if rng.random() >= self.luhn_rate:
                digits = digits[:-1] + str((int(digits[-1]) + rng.randint(1, 9)) % 10)
            return fill_mask_with(self.masks.pick(rng), digits, rng)
        if sem == "postal_code":
            postal = rec["postal"] if rec else f.new_postal()
            return fill_mask_with(self.masks.pick(rng), postal, rng)
        if sem in ("phone", "identifier", "unknown"):
            m = self.masks.pick(rng)
            return fill_mask(m, rng, p.fixed_chars.get(m))
        if sem in ("first_name", "last_name", "full_name"):
            first, middle, last = (rec["first"], rec["middle"], rec["last"]) if rec else f.new_names()
            if sem == "first_name":
                core = first
            elif sem == "last_name":
                core = last
            else:
                core = self.templates.pick(rng).format(first=first, middle=middle, last=last, mi=middle[0])
            return render_text(core, self.styles.pick(rng))
        if sem == "email":
            local = rec["email_local"] if rec else f.new_email_local()
            domain = self.domains.pick(rng) if self.domains else f.new_email_domain()
            return render_text(f"{local}@{domain}", self.styles.pick(rng))
        if sem == "city":
            return render_text(rec["city"] if rec else f.new_city(), self.styles.pick(rng))
        if sem == "street_address":
            return render_text(rec["street"] if rec else f.new_street(), self.styles.pick(rng))
        if sem == "province":
            case, lead, trail, form = self.styles.pick(rng)
            abbr = rec["province"] if rec else f.new_province()
            return render_text(abbr if form == "abbr" else PROVINCES[abbr], (case, lead, trail))
        if sem == "date":
            seconds = rec["dates"][p.index] if rec else self.sampler.sample(rng)
            fmt, case, lead, trail = self.formats.pick(rng)
            when = EPOCH + dt.timedelta(seconds=round(seconds))
            return render_text(when.strftime(fmt), (case, lead, trail))
        if sem == "numeric":
            fmt, lead, trail = self.formats.pick(rng)
            value = self.sampler.sample(rng)
            return f"{lead}{format_number(value, fmt)}{trail}"
        if sem == "category":
            choice = self.categories.pick(rng)
            return fill_mask(choice.mask, rng) if isinstance(choice, _RareMask) else choice
        if sem == "free_text":
            return render_text(self._text(rng.choice(p.lengths)), self.styles.pick(rng))
        return None

    def _text(self, length: int) -> str:
        if length < 5:
            return fill_mask("a" * length, self.rng)
        text = self.fake.text(max_nb_chars=max(length, 5)).replace("\n", " ")
        return text[:length].rstrip()


def _ascii_word(s: str) -> str:
    """'Labonté' -> 'labonte' (for e-mail user names)."""
    return re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower())


class _RareMask:
    """Marks a category option that stands for 'some rare value with this mask'."""
    __slots__ = ("mask",)

    def __init__(self, mask: str):
        self.mask = mask


def _merged(profiles: list[ColumnProfile], attr: str) -> Counter:
    total: Counter = Counter()
    for p in profiles:
        total.update(getattr(p, attr))
    return total


class Synthesizer:
    def __init__(self, profiles: list[ColumnProfile], date_groups: list[list[int]],
                 guard: LeakGuard, seed=None, locale: str = "en_CA", rows: int = 10_000):
        self.rng = random.Random(seed)
        self.fake = Faker(locale)
        if seed is not None:
            self.fake.seed_instance(seed)
        pool_size = min(3000, max(300, 2 * rows))
        self.factory = RecordFactory(profiles, date_groups, self.fake, self.rng, guard, pool_size)
        self.columns = [ColumnGenerator(p, self.factory, self.fake, self.rng, guard) for p in profiles]

    def generate(self, n: int) -> list[list]:
        cols: list[list] = [[] for _ in self.columns]
        for _ in range(n):
            rec = self.factory.make()
            for out, gen in zip(cols, self.columns):
                out.append(gen.cell(rec))
        return cols
