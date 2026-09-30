"""
guard.py
~~~~~~~~
The leak guard: the last line of defence that stops a real value from the
source file turning up in the synthetic file.

Every real value in a sensitive column of the source is turned into a few
normalised "keys" (case/whitespace-insensitive, punctuation-insensitive, and
word-order-insensitive), and every generated value for a sensitive column is
checked against them. A generated value that matches is regenerated; if it
keeps matching, it is replaced by random characters of the same shape.

Values shorter than MIN_LENGTH characters are not guarded: a 1-2 character
value ("M", "QC", "12") can't identify anyone and would otherwise make the
guard impossible to satisfy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

MIN_LENGTH = 3
_NON_ALNUM = re.compile(r"[\W_]+")
_WORD = re.compile(r"[^\W_]+")


class LeakError(RuntimeError):
    """Raised in --strict mode when a real value could not be kept out of the output."""


@dataclass
class GuardStats:
    collisions: int = 0   # generated values that matched a real value
    fallbacks: int = 0    # ... and needed the random-characters fallback
    unresolved: int = 0   # ... and still matched (only possible for tiny value spaces)


def _keys(value) -> list[int]:
    s = " ".join((value if type(value) is str else str(value)).split()).casefold()
    if len(s) < MIN_LENGTH:
        return []
    # Store 64-bit hashes rather than the real strings themselves.
    if s.isalnum():
        return [hash("=" + s), hash("#" + s)]
    keys = [hash("=" + s)]
    alnum = _NON_ALNUM.sub("", s)
    if len(alnum) >= MIN_LENGTH:
        keys.append(hash("#" + alnum))
    if " " in s:
        words = _WORD.findall(s)
        if len(words) > 1:
            keys.append(hash("~" + " ".join(sorted(words))))
    return keys


class LeakGuard:
    def __init__(self, retries: int = 25, strict: bool = False):
        self._real: set[int] = set()
        self.retries = retries
        self.strict = strict
        self.stats: dict[str, GuardStats] = {}

    def add_real(self, value) -> None:
        if value is not None:
            self._real.update(_keys(value))

    def __len__(self) -> int:
        return len(self._real)

    def is_leak(self, value) -> bool:
        if value is None:
            return False
        return any(k in self._real for k in _keys(value))

    def record(self, label: str, collisions: int = 0) -> None:
        self.stats.setdefault(label, GuardStats()).collisions += collisions

    def check(
        self,
        label: str,
        value,
        regenerate: Callable[[], object],
        fallback: Callable[[object], object],
    ):
        """Return `value`, or a replacement for it if it matches a real value."""
        if not self.is_leak(value):
            return value
        st = self.stats.setdefault(label, GuardStats())
        st.collisions += 1
        for _ in range(self.retries):
            value = regenerate()
            if not self.is_leak(value):
                return value
        st.fallbacks += 1
        for _ in range(self.retries):
            value = fallback(value)
            if not self.is_leak(value):
                return value
        st.unresolved += 1
        if self.strict:
            raise LeakError(
                f"Column '{label}': could not generate a value that differs from every "
                f"real value (the column's value space is too small). Re-run without "
                f"--strict, or override this column's type."
            )
        return value
