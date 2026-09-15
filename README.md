# fakegen

A synthetic (fake) data generator for teams who need **full control** over
the shape of the data they generate: exactly which columns, exactly how
many rows, exactly which values are allowed in each column — defined in a
plain YAML (or spreadsheet-style CSV) file, so non-programmers on the team
can configure it without touching Python.

It's built on [Faker](https://faker.readthedocs.io/) for realistic-looking
fake values (names, emails, addresses, dates, ...) and
[DuckDB](https://duckdb.org) for reading real reference files efficiently.

## Table of contents

- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [Key concepts](#key-concepts)
- [The two ways to build a schema](#the-two-ways-to-build-a-schema)
  - [A) Hand-write / spreadsheet it](#a-hand-write--spreadsheet-it)
  - [B) Infer it from a real reference file](#b-infer-it-from-a-real-reference-file)
- [Column types reference](#column-types-reference)
- [All commands](#all-commands)
- [Privacy notes](#privacy-notes)
- [Using it from Python](#using-it-from-python)
- [Project layout](#project-layout)
- [Running the tests](#running-the-tests)
- [Troubleshooting](#troubleshooting)

## Prerequisites

1. **Python 3.12 or newer** — check with `python3 --version`.
2. **[uv](https://docs.astral.sh/uv/)** — this project's package manager.
   Install once with:

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

You don't need to manually create a virtual environment or `pip install`
anything — `uv sync` (next step) handles that.

## Quick start

Run these from inside the `create_fake_data/` folder (the one this README
is in):

```bash
# 1. Install dependencies into a local virtual environment (.venv/)
uv sync

# 2. Scaffold an example schema to start from
uv run fakegen init

# 3. Check it's valid
uv run fakegen validate --schema schema.yaml

# 4. Generate the data
uv run fakegen generate --schema schema.yaml --out-dir data

# 5. Look at what you got
head data/synthetic.csv
```

`uv run` runs a command inside this project's own virtual environment, so
you never need to "activate" anything manually.

## Key concepts

| Term | What it means here |
|------|---------------------|
| **Schema** | A YAML file describing exactly what to generate: how many rows (`dataset.rows`), and the exact ordered list of columns (`columns:`), each with a `type` that says how to fill it. |
| **Column type** | How a column's values are produced — a Faker provider (realistic fake names/emails/etc.), a fixed code set (e.g. `["Active", "Withdrawn"]`), a numeric or date range, a sequence, a constant, or values reused from a real file. Full list in [Column types reference](#column-types-reference). |
| **Faker / provider** | [Faker](https://faker.readthedocs.io/en/master/providers.html) is a Python library with hundreds of built-in "providers" — methods like `name()`, `email()`, `address()`, `random_int()` — that produce realistic-looking fake values. In a schema you reference these by name, e.g. `provider: email`. |
| **Code set** | A fixed list of allowed values for a column, e.g. treatment arms, status codes, region names — the kind of thing SAS/clinical teams usually call a "code list". |
| **Reference file** | A real dataset (`.csv`/`.parquet`) you point the tool at so it can either (a) learn a safe generation strategy for each column (`fakegen infer`), or (b) literally reuse some of its values (`passthrough` columns) — see [Privacy notes](#privacy-notes). |
| **`fakegen infer`** | Reads an *exact* set of columns and an *exact* row window from a reference file and writes a starter schema for you, so you don't have to write one from scratch by hand. |

## The two ways to build a schema

### A) Hand-write / spreadsheet it

Write the YAML directly (see `examples/example_schema.yaml`), or — if your
team would rather work in a spreadsheet — fill in a CSV with one row per
column and convert it:

```bash
uv run fakegen csv-to-yaml \
    --csv examples/example_columns.csv \
    --out schema.yaml \
    --rows 5000 \
    --formats csv parquet
```

See `examples/example_columns.csv` for the exact column headers it
understands (`name`, `type`, `provider`, `kwargs_json`, `values`,
`weights`, `min`, `max`, `start`, `end`, `value`, `source_column`,
`missing_pct`, `unique`, `dtype`). Only fill in the columns each row's
`type` actually needs — leave the rest blank.

### B) Infer it from a real reference file

If your team has a real dataset and wants fake data shaped like it —
**without copying real people's data** — point `infer` at the exact
columns and exact row range you want it to look at:

```bash
uv run fakegen infer \
    --source real_patients.parquet \
    --columns site_region age treatment_arm enrollment_date \
    --rows 2000 \
    --row-offset 0 \
    --out schema.yaml
```

This reads *only* those 4 columns and *only* those 2000 rows (DuckDB
pushes the column/row selection down to the file itself, so it works fine
even if `real_patients.parquet` has millions of rows and hundreds of other
columns you didn't ask for).

**Don't have a clean list of columns to start from?** Leave `--columns`
out entirely and `infer` will auto-detect every column in the file:

```bash
uv run fakegen infer --source real_patients.parquet --rows 2000 --out schema.yaml
```

This is a good default when the source is messy or you just want a
starting point covering everything that's there — review the output and
delete any columns you don't actually need before generating.

For each column it decides:

- **Numeric column** → a `numeric_range` using the observed min/max (no real numbers copied, just the range).
- **Date column** → a `date_range`, same idea.
- **Low-cardinality text/boolean column** (few distinct values in the sample, e.g. a status or region code) → a `code_set` listing those distinct values.
- **High-cardinality text column** (looks like free text, names, or IDs) → left as a `needs_mapping` placeholder — **`generate` will refuse to run** until you edit it to something explicit. This is deliberate: the tool never silently reuses values that might be sensitive.

After inferring, open `schema.yaml`, fix any `needs_mapping` columns (point
them at a Faker provider, a manually curated `code_set`, or an explicit
`passthrough` — see below), then run `generate` as normal.

## Column types reference

| `type` | Required keys | What it produces |
|--------|---------------|-------------------|
| `sequence` | *(optional)* `start` (default 1), `step` (default 1) | Auto-incrementing integer — good for IDs. |
| `constant` | `value` | The same fixed value on every row. |
| `code_set` | `values` (a list) **or** `values_file` (path to a one-value-per-line/CSV file); optional `weights` (must match `values` length; default uniform) | A random pick from a fixed list of allowed values. |
| `numeric_range` | `min`, `max`; optional `dtype: int` or `dtype: float` | A random number in that range. |
| `date_range` | `start`, `end` (both `YYYY-MM-DD`) | A random date in that range, as an ISO date string. |
| `faker` | `provider` (a [Faker method name](https://faker.readthedocs.io/en/master/providers.html)); optional `kwargs` (passed to that method) | Whatever that Faker provider returns, e.g. `provider: email`, `provider: random_int` with `kwargs: {min: 1, max: 100}`. |
| `passthrough` | `source_column` (a column name in the reference file) | A real value **sampled with replacement** from that column of the reference file. Requires `dataset.reference` (or `--reference` on the command line). See [Privacy notes](#privacy-notes). |
| `needs_mapping` | — | A placeholder `fakegen infer` leaves for columns it couldn't confidently handle. `generate` refuses to run while any column has this type. |

Any column can also set:

- `missing_pct: 15` — randomly set ~15% of that column's values to null.
- `unique: true` — best-effort uniqueness within the generated column (raises a clear error if it can't find enough distinct values).
- `dtype: int|float|string|date|bool` — a hint used by a couple of types (currently `numeric_range`) to pick integer vs. decimal output.

## All commands

Run `uv run fakegen <command> --help` for the full flag list of any
command.

| Command | Purpose |
|---------|---------|
| `fakegen init` | Writes `schema.yaml` and `columns.csv` example files to start from. |
| `fakegen infer` | Builds a starter schema from an exact column/row slice of a real file. |
| `fakegen csv-to-yaml` | Converts a spreadsheet-style column-catalog CSV into a schema YAML. |
| `fakegen validate` | Checks a schema file for errors without generating anything. |
| `fakegen generate` | Generates the dataset(s) a schema describes, to CSV and/or Parquet. |

Useful `generate` flags: `--rows N` (override the row count without
editing the YAML), `--out-dir DIR`, `--formats csv parquet` (write both at
once), `--reference FILE` (for `passthrough` columns), `--chunk-size N`
(rows processed per batch — lower it if you're generating a very large
file on a memory-constrained machine; default 50,000).

## Privacy notes

This tool is deliberately conservative by default:

- `fakegen infer` **never** proposes copying real values for anything that
  looks like free text or an identifier — those come back as
  `needs_mapping` and must be dealt with manually.
- The only way to make generated data reuse real values at all is the
  explicit, opt-in `passthrough` column type, and even then it samples
  *with replacement* from the reference column you name — it does not
  preserve or expose whole real records, row relationships across columns,
  or anything not explicitly listed as `passthrough`.
- Before using `passthrough` on a column, make sure your team is actually
  OK with that specific column's real values appearing in the generated
  file (e.g. a fixed set of real site codes is usually fine; real patient
  names or IDs usually are not — use `faker`/`code_set` for those instead).

## Using it from Python

```python
from fakegen import generate_dataset, load_schema

spec = load_schema("schema.yaml")
stats = generate_dataset(spec, out_dir="data", rows=10_000)
print(stats)  # {"rows": ..., "columns": [...], "files": [...], "elapsed": ...}
```

## Project layout

```
create_fake_data/
├── pyproject.toml          # project metadata & dependencies (read by uv)
├── uv.lock                 # exact locked dependency versions
├── README.md               # this file
├── examples/
│   ├── example_schema.yaml
│   └── example_columns.csv
├── src/fakegen/
│   ├── schema.py            # the config data model, YAML load/dump, validation
│   ├── generate.py          # the chunked generation engine (writes csv/parquet)
│   ├── infer.py             # builds a schema from a slice of a real file
│   ├── csv_schema.py        # CSV column-catalog -> YAML schema conversion
│   ├── providers.py         # validated Faker provider lookup
│   ├── cli.py                # the `fakegen` command-line tool
│   ├── __init__.py
│   └── __main__.py
└── tests/
```

## Running the tests

```bash
uv run pytest
```

## Troubleshooting

**`generate` refuses to run, complaining about `needs_mapping`**
Some column in your schema still has `type: needs_mapping` — this is the
placeholder `fakegen infer` leaves for columns it wasn't confident enough
to auto-fill (usually free text or ID-like columns). Open the YAML and
give it a real type.

**"Unknown Faker provider '...'"**
The `provider:` name doesn't match a real Faker method. Check the spelling
against the [Faker provider list](https://faker.readthedocs.io/en/master/providers.html)
— most are lowercase_with_underscores, e.g. `name`, `email`,
`random_int`, `date_of_birth`.

**A `passthrough` column errors about a missing reference**
Set `reference: path/to/file` under `dataset:` in the YAML, or pass
`--reference path/to/file` to `generate`.

**Generation is slow for a very large row count**
This is expected — Faker produces one value at a time and can't be
vectorised the way NumPy can. Data is still written in bounded-memory
chunks (`--chunk-size`, default 50,000), so it won't crash, just take a
while proportional to the row count and how many `faker`-type columns you
have.
