"""
render.py
~~~~~~~~~
Small helpers for describing the *shape* of a real value and producing a new
random value with the same shape.

A "mask" replaces every digit with 9, every upper-case letter with A and
every lower-case letter with a, and keeps everything else (spaces,
punctuation) as-is:

    "123-456 789"  -> "999-999 999"
    " k1a 0b1"     -> " a9a 9a9"
    "CL-00042"     -> "AA-99999"

A mask carries no real characters, so it is safe to keep and re-fill.
"""

from __future__ import annotations

import random
import re
import string
from typing import Optional

_WORD_RE = re.compile(r"[^\W\d_]+")
_VOWELS = "aeiou"
_CONSONANTS = "bcdfghjklmnprstvwz"


def mask_of(value: str) -> str:
    out = []
    for c in value:
        if c.isdigit():
            out.append("9")
        elif c.isalpha():
            out.append("A" if c.isupper() else "a")
        else:
            out.append(c)
    return "".join(out)


def fill_mask(mask: str, rng: random.Random, fixed: Optional[dict[int, str]] = None) -> str:
    """Random value with the given mask; `fixed` pins some positions to a character."""
    out = []
    for i, c in enumerate(mask):
        if fixed and i in fixed:
            out.append(fixed[i])
        elif c == "9":
            out.append(rng.choice(string.digits))
        elif c == "A":
            out.append(rng.choice(string.ascii_uppercase))
        elif c == "a":
            out.append(rng.choice(string.ascii_lowercase))
        else:
            out.append(c)
    return "".join(out)


def fill_mask_with(mask: str, chars: str, rng: random.Random) -> str:
    """Pour `chars` (e.g. a generated SIN's digits) into the mask's slots in order."""
    it = iter(chars)
    out = []
    for c in mask:
        if c in "9Aa":
            nxt = next(it, None)
            if nxt is None:
                out.append(fill_mask(c, rng))
            elif c == "A":
                out.append(nxt.upper())
            elif c == "a":
                out.append(nxt.lower())
            else:
                out.append(nxt)
        else:
            out.append(c)
    return "".join(out)


def split_ws(value: str) -> tuple[str, str, str]:
    """'  abc ' -> ('  ', 'abc', ' ')"""
    core = value.strip()
    if not core:
        return value, "", ""
    lead = value[: len(value) - len(value.lstrip())]
    trail = value[len(value.rstrip()):]
    return lead[:10], core, trail[:10]


def case_style(core: str) -> str:
    if core.isupper():
        return "upper"
    if core.islower():
        return "lower"
    if core.istitle():
        return "title"
    return "mixed" if any(c.isalpha() for c in core) else "none"


def title_case(s: str) -> str:
    return _WORD_RE.sub(lambda m: m.group(0)[0].upper() + m.group(0)[1:].lower(), s)


def apply_case(s: str, style: str) -> str:
    if style == "upper":
        return s.upper()
    if style == "lower":
        return s.lower()
    if style == "title":
        return title_case(s)
    return s


def text_style(value: str) -> tuple[str, str, str]:
    """(case style, leading whitespace, trailing whitespace) of a raw value."""
    lead, core, trail = split_ws(value)
    return case_style(core), lead, trail


def render_text(core: str, style: tuple[str, str, str]) -> str:
    case, lead, trail = style
    return f"{lead}{apply_case(core, case)}{trail}"


def format_number(value: float, fmt: tuple) -> str:
    currency, thousands, decimals, zero_pad, pct = fmt
    body = f"{abs(value):,.{decimals}f}" if thousands else f"{abs(value):.{decimals}f}"
    if zero_pad and not thousands:
        int_part, _, frac = body.partition(".")
        body = int_part.zfill(zero_pad) + (f".{frac}" if frac else "")
    sign = "-" if value < 0 and float(body.replace(",", "") or 0) != 0 else ""
    return f"{sign}{currency}{body}{pct}"


def pronounceable(rng: random.Random, length: int = 6) -> str:
    """A made-up but name-like word, e.g. 'Tavoki' -- used as a last-resort fallback."""
    chars = []
    for i in range(max(length, 2)):
        chars.append(rng.choice(_CONSONANTS if i % 2 == 0 else _VOWELS))
    return "".join(chars).capitalize()
