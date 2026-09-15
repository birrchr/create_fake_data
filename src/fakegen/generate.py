"""
generate.py
~~~~~~~~~~~
The generation engine: takes a validated DatasetSpec and writes the exact
number of rows and columns it describes to CSV and/or Parquet.

Rows are produced in chunks (default 50,000) so memory use stays bounded
regardless of how many rows are requested -- Faker generates one value at a
time (it can't be vectorised the way NumPy can), so this keeps a large run
slow-but-steady rather than accumulating everything in a single huge list
before writing anything out.
"""

from __future__ import annotations

import csv
import datetime as dt
import random
from pathlib import Path
from typing import Any, Callable, Optional

import duckdb
from faker import Faker

from .providers import call_provider
from .schema import ColumnSpec, DatasetSpec, SchemaError, validate_schema

ProgressCallback = Callable[[int, int], None]  # (rows_done, rows_total) -> None

# Reference-file columns used by `passthrough` are cached up to this many
# rows so a huge reference file doesn't need to be read in full just to
# sample a handful of realistic values from one column.
DEFAULT_REFERENCE_LIMIT = 200_000


def generate_dataset(
    spec: DatasetSpec,
    out_dir: Optional[str | Path] = None,
    rows: Optional[int] = None,
    reference: Optional[str | Path] = None,
    reference_limit: int = DEFAULT_REFERENCE_LIMIT,
    chunk_size: int = 50_000,
    on_chunk: Optional[ProgressCallback] = None,
) -> dict[str, Any]:
    """
    Generate the dataset described by `spec` and write it to disk.

    Returns a stats dict: {"rows": int, "files": [Path, ...], "elapsed": float}
    """
    errors = validate_schema(spec)
    if errors:
        raise SchemaError("Invalid schema:\n  - " + "\n  - ".join(errors))

    total_rows = rows if rows is not None else spec.rows
    out_dir = Path(out_dir) if out_dir else Path(spec.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    reference_path = str(reference) if reference else spec.reference

    fake = Faker(spec.locale)
    if spec.seed is not None:
        Faker.seed(spec.seed)
        fake.seed_instance(spec.seed)
    rng = random.Random(spec.seed)

    reference_cache = _load_reference_columns(spec, reference_path, reference_limit)
    generators = {
        col.name: _make_column_generator(col, fake, rng, reference_cache)
        for col in spec.columns
    }
    unique_seen: dict[str, set] = {c.name: set() for c in spec.columns if c.unique}

    import time
    t0 = time.perf_counter()

    writers = _open_writers(spec, out_dir, [c.name for c in spec.columns])
    try:
        rows_done = 0
        while rows_done < total_rows:
            n = min(chunk_size, total_rows - rows_done)
            chunk = _build_chunk(spec.columns, generators, rng, unique_seen, rows_done, n)
            for writer in writers.values():
                writer.write_chunk(chunk)
            rows_done += n
            if on_chunk:
                on_chunk(rows_done, total_rows)
    finally:
        for writer in writers.values():
            writer.close()

    elapsed = time.perf_counter() - t0
    return {
        "rows": total_rows,
        "columns": [c.name for c in spec.columns],
        "files": [w.path for w in writers.values()],
        "elapsed": elapsed,
    }


def _build_chunk(
    columns: list[ColumnSpec],
    generators: dict[str, Callable[[int], Any]],
    rng: random.Random,
    unique_seen: dict[str, set],
    row_offset: int,
    n: int,
) -> dict[str, list]:
    chunk: dict[str, list] = {c.name: [None] * n for c in columns}
    for col in columns:
        gen = generators[col.name]
        values = chunk[col.name]
        seen = unique_seen.get(col.name)
        for i in range(n):
            row_index = row_offset + i
            if col.missing_pct and rng.random() * 100 < col.missing_pct:
                values[i] = None
                continue
            value = gen(row_index)
            if seen is not None:
                attempts = 0
                while value in seen and attempts < 1000:
                    value = gen(row_index)
                    attempts += 1
                if value in seen:
                    raise SchemaError(
                        f"Column '{col.name}' requires unique values but ran out of "
                        f"distinct options after {row_index + 1} rows. Widen its "
                        f"value range/code set or drop 'unique: true'."
                    )
                seen.add(value)
            values[i] = value
    return chunk


# ---------------------------------------------------------------------------
# Per-column value generators
# ---------------------------------------------------------------------------

def _make_column_generator(
    col: ColumnSpec,
    fake: Faker,
    rng: random.Random,
    reference_cache: dict[str, list],
) -> Callable[[int], Any]:
    p = col.params

    if col.type == "sequence":
        start = int(p.get("start", 1))
        step = int(p.get("step", 1))
        return lambda i: start + i * step

    if col.type == "constant":
        value = p["value"]
        return lambda i: value

    if col.type == "code_set":
        values = p.get("values")
        if values is None:
            values = _read_values_file(p["values_file"])
        weights = p.get("weights")
        return lambda i: rng.choices(values, weights=weights, k=1)[0]

    if col.type == "numeric_range":
        lo, hi = p["min"], p["max"]
        if col.dtype == "int" or (isinstance(lo, int) and isinstance(hi, int)):
            return lambda i: rng.randint(int(lo), int(hi))
        return lambda i: rng.uniform(float(lo), float(hi))

    if col.type == "date_range":
        start = _parse_date(p["start"])
        end = _parse_date(p["end"])
        span_days = (end - start).days
        return lambda i: (start + dt.timedelta(days=rng.randint(0, max(span_days, 0)))).isoformat()

    if col.type == "faker":
        provider = p["provider"]
        kwargs = p.get("kwargs", {}) or {}
        return lambda i: call_provider(fake, provider, kwargs)

    if col.type == "passthrough":
        pool = reference_cache.get(col.params["source_column"], [])
        if not pool:
            raise SchemaError(
                f"Column '{col.name}': no values loaded from reference column "
                f"'{col.params['source_column']}' -- check the column name and "
                f"that --reference points at the right file."
            )
        return lambda i: rng.choice(pool)

    raise SchemaError(f"Column '{col.name}': cannot generate values for type '{col.type}'")


def _parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(str(value))


def _read_values_file(path: str) -> list[str]:
    p = Path(path)
    if p.suffix.lower() == ".csv":
        import csv as _csv
        with open(p, newline="", encoding="utf-8") as fh:
            reader = _csv.reader(fh)
            return [row[0] for row in reader if row]
    with open(p, encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip()]


# ---------------------------------------------------------------------------
# Reference-file loading (for `passthrough` columns)
# ---------------------------------------------------------------------------

def _load_reference_columns(
    spec: DatasetSpec, reference_path: Optional[str], limit: int
) -> dict[str, list]:
    passthrough_cols = [c for c in spec.columns if c.type == "passthrough"]
    if not passthrough_cols:
        return {}
    if not reference_path:
        raise SchemaError(
            "Config has 'passthrough' columns but no reference file was given "
            "(set dataset.reference in the YAML or pass --reference)."
        )

    source_cols = sorted({c.params["source_column"] for c in passthrough_cols})
    con = duckdb.connect()
    try:
        rel = _read_source(con, reference_path)
        cols_sql = ", ".join(f'"{c}"' for c in source_cols)
        df = con.execute(f"SELECT {cols_sql} FROM {rel} LIMIT {int(limit)}").df()
    finally:
        con.close()

    return {col: df[col].dropna().tolist() for col in source_cols}


def _read_source(con: duckdb.DuckDBPyConnection, path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".parquet":
        con.execute(f"CREATE TEMP VIEW _ref AS SELECT * FROM read_parquet('{path}')")
    elif suffix == ".csv":
        con.execute(f"CREATE TEMP VIEW _ref AS SELECT * FROM read_csv_auto('{path}')")
    else:
        raise SchemaError(f"Unsupported reference file type: {path} (use .parquet or .csv)")
    return "_ref"


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

class _CsvWriter:
    def __init__(self, path: Path, fieldnames: list[str]):
        self.path = path
        self._fh = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=fieldnames)
        self._writer.writeheader()

    def write_chunk(self, chunk: dict[str, list]) -> None:
        n = len(next(iter(chunk.values())))
        for i in range(n):
            self._writer.writerow({k: chunk[k][i] for k in chunk})

    def close(self) -> None:
        self._fh.close()


class _ParquetWriter:
    def __init__(self, path: Path, fieldnames: list[str]):
        import pyarrow as pa
        self.path = path
        self._fieldnames = fieldnames
        self._pa = pa
        self._writer = None  # opened lazily once we see the first chunk's types

    def write_chunk(self, chunk: dict[str, list]) -> None:
        import pyarrow.parquet as pq
        table = self._pa.table({k: chunk[k] for k in self._fieldnames})
        if self._writer is None:
            self._writer = pq.ParquetWriter(str(self.path), table.schema)
        self._writer.write_table(table)

    def close(self) -> None:
        if self._writer is not None:
            self._writer.close()


def _open_writers(spec: DatasetSpec, out_dir: Path, fieldnames: list[str]) -> dict[str, Any]:
    writers: dict[str, Any] = {}
    for fmt in spec.formats:
        fmt = fmt.lower()
        path = out_dir / f"{spec.file_stem}.{fmt}"
        if fmt == "csv":
            writers["csv"] = _CsvWriter(path, fieldnames)
        elif fmt == "parquet":
            writers["parquet"] = _ParquetWriter(path, fieldnames)
        else:
            raise SchemaError(f"Unsupported output format: '{fmt}' (use csv or parquet)")
    return writers
