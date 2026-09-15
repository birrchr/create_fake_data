"""
schema.py
~~~~~~~~~
The data model for a fakegen YAML config, plus loading/validation.

A config has two parts:

  dataset:   run-level settings (row count, seed, output formats, ...)
  columns:   an ordered list of column definitions -- this list IS the
             exact set of output columns, in the exact order they'll
             appear in the generated file(s).

Each column has a `type` that determines which other keys it needs:

  sequence     -- auto-incrementing integer (start, step)
  constant     -- the same fixed value on every row (value)
  code_set     -- a fixed list of allowed values, optionally weighted
                  (values or values_file, weights)
  numeric_range-- a random number in [min, max] (min, max, dtype: int|float)
  date_range   -- a random date in [start, end] (start, end -- "YYYY-MM-DD")
  faker        -- calls a method on Python's Faker (provider, kwargs)
  passthrough  -- reuses real values sampled from a reference file
                  (source_column) -- see the privacy note in the README
                  before using this one.
  needs_mapping-- placeholder emitted by `fakegen infer` for columns it
                  couldn't confidently infer a safe strategy for. `generate`
                  refuses to run until these are replaced by a real type.

Any column may also set:
  missing_pct  -- % of values to randomly set to null (default 0)
  unique       -- best-effort uniqueness within the generated column
  dtype        -- output type hint: "int" | "float" | "string" | "date" | "bool"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

VALID_TYPES = {
    "sequence", "constant", "code_set", "numeric_range",
    "date_range", "faker", "passthrough", "needs_mapping",
}

_COMMON_KEYS = {"name", "type", "missing_pct", "unique", "dtype"}


class SchemaError(ValueError):
    """Raised for structurally or semantically invalid configs."""


@dataclass
class ColumnSpec:
    name: str
    type: str
    missing_pct: float = 0.0
    unique: bool = False
    dtype: Optional[str] = None
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ColumnSpec":
        if "name" not in raw:
            raise SchemaError(f"Column definition missing 'name': {raw}")
        if "type" not in raw:
            raise SchemaError(f"Column '{raw['name']}' missing 'type'")
        return cls(
            name=raw["name"],
            type=raw["type"],
            missing_pct=float(raw.get("missing_pct", 0.0)),
            unique=bool(raw.get("unique", False)),
            dtype=raw.get("dtype"),
            params={k: v for k, v in raw.items() if k not in _COMMON_KEYS},
        )

    def to_dict(self) -> dict[str, Any]:
        out = {"name": self.name, "type": self.type, **self.params}
        if self.missing_pct:
            out["missing_pct"] = self.missing_pct
        if self.unique:
            out["unique"] = self.unique
        if self.dtype:
            out["dtype"] = self.dtype
        return out


@dataclass
class DatasetSpec:
    rows: int = 1000
    seed: Optional[int] = None
    locale: str = "en_US"
    formats: list[str] = field(default_factory=lambda: ["csv"])
    output_dir: str = "data"
    file_stem: str = "synthetic"
    reference: Optional[str] = None
    columns: list[ColumnSpec] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DatasetSpec":
        ds = raw.get("dataset", {}) or {}
        cols_raw = raw.get("columns")
        if not cols_raw:
            raise SchemaError("Config has no 'columns' list.")
        columns = [ColumnSpec.from_dict(c) for c in cols_raw]
        return cls(
            rows=int(ds.get("rows", 1000)),
            seed=ds.get("seed"),
            locale=ds.get("locale", "en_US"),
            formats=list(ds.get("formats", ["csv"])),
            output_dir=ds.get("output_dir", "data"),
            file_stem=ds.get("file_stem", "synthetic"),
            reference=ds.get("reference"),
            columns=columns,
        )

    def to_dict(self) -> dict[str, Any]:
        ds = {
            "rows": self.rows,
            "formats": self.formats,
            "output_dir": self.output_dir,
            "file_stem": self.file_stem,
        }
        if self.seed is not None:
            ds["seed"] = self.seed
        if self.locale != "en_US":
            ds["locale"] = self.locale
        if self.reference:
            ds["reference"] = self.reference
        return {"dataset": ds, "columns": [c.to_dict() for c in self.columns]}


def load_schema(path: str | Path) -> DatasetSpec:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not raw:
        raise SchemaError(f"{path} is empty or not valid YAML.")
    return DatasetSpec.from_dict(raw)


def dump_schema(spec: DatasetSpec, path: str | Path) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(spec.to_dict(), fh, sort_keys=False, allow_unicode=True)


def validate_schema(spec: DatasetSpec) -> list[str]:
    """Return a list of human-readable errors; empty list means valid."""
    errors: list[str] = []

    if spec.rows <= 0:
        errors.append(f"dataset.rows must be > 0 (got {spec.rows})")

    names_seen: set[str] = set()
    needs_reference = False

    for col in spec.columns:
        if col.name in names_seen:
            errors.append(f"Duplicate column name: '{col.name}'")
        names_seen.add(col.name)

        if col.type not in VALID_TYPES:
            errors.append(
                f"Column '{col.name}': unknown type '{col.type}'. "
                f"Valid types: {', '.join(sorted(VALID_TYPES))}"
            )
            continue

        if col.type == "needs_mapping":
            errors.append(
                f"Column '{col.name}' is still a placeholder from `fakegen infer` "
                f"-- edit it to a real type (faker / code_set / numeric_range / "
                f"date_range / passthrough) before generating."
            )

        if not (0 <= col.missing_pct <= 100):
            errors.append(f"Column '{col.name}': missing_pct must be 0-100 (got {col.missing_pct})")

        if col.type == "code_set":
            has_values = "values" in col.params
            has_file = "values_file" in col.params
            if not has_values and not has_file:
                errors.append(f"Column '{col.name}': code_set needs 'values' or 'values_file'")
            weights = col.params.get("weights")
            values = col.params.get("values")
            if weights is not None and values is not None and len(weights) != len(values):
                errors.append(
                    f"Column '{col.name}': 'weights' length ({len(weights)}) != "
                    f"'values' length ({len(values)})"
                )

        elif col.type == "numeric_range":
            if "min" not in col.params or "max" not in col.params:
                errors.append(f"Column '{col.name}': numeric_range needs 'min' and 'max'")
            elif col.params["min"] > col.params["max"]:
                errors.append(f"Column '{col.name}': min > max")

        elif col.type == "date_range":
            if "start" not in col.params or "end" not in col.params:
                errors.append(f"Column '{col.name}': date_range needs 'start' and 'end' (YYYY-MM-DD)")

        elif col.type == "faker":
            if "provider" not in col.params:
                errors.append(f"Column '{col.name}': faker needs 'provider' (a Faker method name)")

        elif col.type == "constant":
            if "value" not in col.params:
                errors.append(f"Column '{col.name}': constant needs 'value'")

        elif col.type == "sequence":
            pass  # start/step are optional, default to 1/1

        elif col.type == "passthrough":
            if "source_column" not in col.params:
                errors.append(f"Column '{col.name}': passthrough needs 'source_column'")
            needs_reference = True

    if needs_reference and not spec.reference:
        errors.append(
            "One or more columns use type 'passthrough' but dataset.reference "
            "is not set (pass --reference on the command line, or add "
            "'reference: path/to/file' under 'dataset:' in the YAML)."
        )

    return errors
