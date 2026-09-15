"""
csv_schema.py
~~~~~~~~~~~~~
Lets a team define columns in a spreadsheet instead of hand-writing YAML,
then converts that CSV into a fakegen schema file.

One row per output column. Recognised headers (all optional except `name`
and `type` -- which fields are actually required depends on `type`, same
as in the YAML schema -- see schema.py's module docstring):

    name, type, provider, kwargs_json, values, weights, values_file,
    min, max, start, end, value, source_column, start_seq, step,
    missing_pct, unique, dtype

`values` and `weights` are pipe-separated within a single CSV cell, e.g.
    values: Placebo|DrugA|DrugB
    weights: 0.34|0.33|0.33

`kwargs_json` is a JSON object string for `faker`-type columns, e.g.
    kwargs_json: {"min": 18, "max": 90}
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Optional

from .schema import ColumnSpec, DatasetSpec, dump_schema

_NUMERIC_FIELDS = {"min", "max", "start", "step", "missing_pct"}
_LIST_FIELDS = {"values", "weights"}


def csv_to_yaml(
    csv_path: str | Path,
    out_path: str | Path,
    rows: int = 1000,
    seed: Optional[int] = None,
    locale: str = "en_US",
    formats: Optional[list[str]] = None,
    output_dir: str = "data",
    file_stem: str = "synthetic",
    reference: Optional[str] = None,
) -> DatasetSpec:
    columns = [_row_to_column(row) for row in _read_rows(csv_path)]
    spec = DatasetSpec(
        rows=rows,
        seed=seed,
        locale=locale,
        formats=formats or ["csv"],
        output_dir=output_dir,
        file_stem=file_stem,
        reference=reference,
        columns=columns,
    )
    dump_schema(spec, out_path)
    return spec


def _read_rows(csv_path: str | Path) -> list[dict[str, str]]:
    with open(csv_path, newline="", encoding="utf-8") as fh:
        return [row for row in csv.DictReader(fh)]


def _row_to_column(row: dict[str, str]) -> ColumnSpec:
    row = {k.strip(): v.strip() for k, v in row.items() if v is not None}
    row = {k: v for k, v in row.items() if v != ""}

    if "name" not in row:
        raise ValueError(f"CSV row missing 'name': {row}")
    if "type" not in row:
        raise ValueError(f"CSV row for column '{row['name']}' missing 'type'")

    name = row.pop("name")
    col_type = row.pop("type")
    missing_pct = float(row.pop("missing_pct", 0) or 0)
    unique = row.pop("unique", "").lower() in ("true", "1", "yes")
    dtype = row.pop("dtype", None)

    params: dict[str, Any] = {}

    if "kwargs_json" in row:
        params["kwargs"] = json.loads(row.pop("kwargs_json"))

    for field_name in list(row):
        value = row[field_name]
        if field_name in _LIST_FIELDS:
            params[field_name] = [v.strip() for v in value.split("|")]
        elif field_name in _NUMERIC_FIELDS:
            params[field_name] = _num(value)
        else:
            params[field_name] = value
        row.pop(field_name)

    if "weights" in params:
        params["weights"] = [float(w) for w in params["weights"]]
    if "values" in params:
        params["values"] = [_num_maybe(v) for v in params["values"]]

    return ColumnSpec(name=name, type=col_type, missing_pct=missing_pct, unique=unique, dtype=dtype, params=params)


def _num(value: str) -> float | int:
    try:
        f = float(value)
        return int(f) if f.is_integer() else f
    except ValueError:
        return value


def _num_maybe(value: str):
    try:
        f = float(value)
        return int(f) if f.is_integer() else f
    except ValueError:
        return value
