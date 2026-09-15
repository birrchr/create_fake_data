"""
infer.py
~~~~~~~~
Build a starter fakegen schema by inspecting an exact slice (specific
columns, specific row window) of a real reference dataset.

Privacy note
------------
This module never copies real values into the emitted schema for anything
it can't confidently call "just a category". Concretely:

  - Numeric columns  -> `numeric_range` using the observed min/max only.
  - Date columns     -> `date_range` using the observed min/max only.
  - Low-cardinality string/bool columns (<= `threshold` distinct values in
    the sampled rows) -> `code_set` using those distinct values. This is
    meant for things like status codes, site IDs, treatment arms -- values
    that describe a category, not a person.
  - High-cardinality string columns (more than `threshold` distinct values)
    -> a `needs_mapping` placeholder. This is the common shape of free text,
    names, emails, and other identifiers, which real values should not be
    reused for. `fakegen generate` refuses to run until you replace these
    with a `faker` provider, a manually curated `code_set`, or an explicit,
    deliberate `passthrough` (which does reuse real values -- opt in only).

Only the exact columns and row range you ask for are ever read from the
reference file (via DuckDB's LIMIT/OFFSET pushdown), so this is safe to
point at a reference file far larger than you want to load.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import duckdb
import pandas as pd
import yaml

from .schema import ColumnSpec, DatasetSpec


def infer_schema(
    source: str | Path,
    columns: Optional[list[str]] = None,
    rows: int = 1000,
    row_offset: int = 0,
    threshold: int = 50,
    weighted: bool = False,
) -> tuple[DatasetSpec, list[str]]:
    """
    Returns (spec, notes) -- `notes` lists which columns need manual
    attention (i.e. came back as `needs_mapping`).

    If `columns` is None, every column found in `source` is used --
    handy when you don't have a clean schema to work from and just want
    a starting point covering everything that's there.
    """
    if columns is None:
        columns = discover_columns(source)

    df = _read_slice(source, columns, rows, row_offset)

    col_specs: list[ColumnSpec] = []
    notes: list[str] = []

    for name in columns:
        series = df[name]
        col_specs.append(_infer_column(name, series, threshold, weighted, notes))

    spec = DatasetSpec(rows=rows, columns=col_specs)
    return spec, notes


def write_inferred_schema(spec: DatasetSpec, notes: list[str], source: str, columns: list[str], path: str | Path) -> None:
    """Write `spec` to `path` as YAML with a header comment explaining what was inferred."""
    header_lines = [
        f"# Schema inferred by `fakegen infer` from: {source}",
        f"# Columns sampled: {', '.join(columns)}",
        f"# Rows sampled: {spec.rows}",
        "#",
        "# Review every column below before generating data from this file.",
    ]
    if notes:
        header_lines.append("#")
        header_lines.append("# Needs your attention (left as `needs_mapping`):")
        header_lines += [f"#   - {n}" for n in notes]
    header_lines.append("")

    body = yaml.safe_dump(spec.to_dict(), sort_keys=False, allow_unicode=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(header_lines) + "\n" + body)


def _infer_column(name: str, series: pd.Series, threshold: int, weighted: bool, notes: list[str]) -> ColumnSpec:
    non_null = series.dropna()
    n_distinct = int(non_null.nunique())

    if pd.api.types.is_bool_dtype(series):
        return _code_set_from_values(name, non_null, weighted)

    if pd.api.types.is_datetime64_any_dtype(series):
        if non_null.empty:
            notes.append(f"'{name}': no non-null sample values to infer a date range from")
            return ColumnSpec(name=name, type="needs_mapping")
        return ColumnSpec(
            name=name,
            type="date_range",
            params={
                "start": non_null.min().date().isoformat(),
                "end": non_null.max().date().isoformat(),
            },
        )

    if pd.api.types.is_numeric_dtype(series):
        if non_null.empty:
            notes.append(f"'{name}': no non-null sample values to infer a numeric range from")
            return ColumnSpec(name=name, type="needs_mapping")
        is_int = pd.api.types.is_integer_dtype(series) or (non_null == non_null.round()).all()
        lo, hi = non_null.min(), non_null.max()
        return ColumnSpec(
            name=name,
            type="numeric_range",
            dtype="int" if is_int else "float",
            params={"min": int(lo) if is_int else float(lo), "max": int(hi) if is_int else float(hi)},
        )

    # Treat everything else (object/string) as categorical-or-not by cardinality.
    if n_distinct == 0:
        notes.append(f"'{name}': every sampled value was null -- cannot infer a strategy")
        return ColumnSpec(name=name, type="needs_mapping")

    if n_distinct <= threshold:
        return _code_set_from_values(name, non_null, weighted)

    notes.append(
        f"'{name}': {n_distinct} distinct values in the sample (looks like free text "
        f"or an identifier) -- left as needs_mapping. Point it at a Faker provider, "
        f"a manually curated code_set, or an explicit passthrough."
    )
    return ColumnSpec(name=name, type="needs_mapping")


def _code_set_from_values(name: str, non_null: pd.Series, weighted: bool) -> ColumnSpec:
    counts = non_null.value_counts()
    values = [_jsonable(v) for v in counts.index.tolist()]
    params: dict = {"values": values}
    if weighted:
        total = counts.sum()
        params["weights"] = [round(float(c) / float(total), 6) for c in counts.tolist()]
    return ColumnSpec(name=name, type="code_set", params=params)


def _jsonable(v):
    if isinstance(v, (bool, int, float, str)) or v is None:
        return v
    return str(v)


def discover_columns(source: str | Path) -> list[str]:
    """Return every column name found in `source`, in file order."""
    con = duckdb.connect()
    try:
        _create_source_view(con, source)
        return [row[0] for row in con.execute("DESCRIBE _src").fetchall()]
    finally:
        con.close()


def _read_slice(source: str | Path, columns: list[str], rows: int, row_offset: int) -> pd.DataFrame:
    con = duckdb.connect()
    try:
        _create_source_view(con, source)
        cols_sql = ", ".join(f'"{c}"' for c in columns)
        return con.execute(
            f"SELECT {cols_sql} FROM _src OFFSET {int(row_offset)} LIMIT {int(rows)}"
        ).df()
    finally:
        con.close()


def _create_source_view(con: duckdb.DuckDBPyConnection, source: str | Path) -> None:
    suffix = Path(str(source)).suffix.lower()
    if suffix == ".parquet":
        con.execute(f"CREATE TEMP VIEW _src AS SELECT * FROM read_parquet('{source}')")
    elif suffix == ".csv":
        con.execute(f"CREATE TEMP VIEW _src AS SELECT * FROM read_csv_auto('{source}')")
    else:
        raise ValueError(f"Unsupported reference file type: {source} (use .parquet or .csv)")
