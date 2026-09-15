"""
providers.py
~~~~~~~~~~~~
Thin, validated wrapper around calling a Faker provider method by name, so
config errors ("typo'd a provider name") surface as a clear message instead
of an AttributeError deep in the generation loop.
"""

from __future__ import annotations

from typing import Any

from faker import Faker


def is_known_provider(fake: Faker, provider: str) -> bool:
    return hasattr(fake, provider) and callable(getattr(fake, provider))


def call_provider(fake: Faker, provider: str, kwargs: dict[str, Any] | None = None) -> Any:
    if not is_known_provider(fake, provider):
        raise ValueError(
            f"Unknown Faker provider '{provider}'. See "
            f"https://faker.readthedocs.io/en/master/providers.html for the "
            f"full list of available provider method names."
        )
    method = getattr(fake, provider)
    return method(**(kwargs or {}))
