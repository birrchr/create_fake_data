# fakegen

A synthetic (fake) data generator for teams who need **full control** over
the shape of the data they generate: exactly which columns, exactly how
many rows, exactly which values are allowed in each column — defined in a
plain YAML (or spreadsheet-style CSV) file, so non-programmers on the team
can configure it without touching Python.

It's built on [Faker](https://faker.readthedocs.io/) for realistic-looking
fake values (names, emails, addresses, dates, ...) and
[DuckDB](https://duckdb.org) for reading real reference files efficiently.

It also has a second, fully automatic mode, **`fakegen synth`**: point it at
a real, messy, sensitive file (or a whole folder of them) and it writes a
`synth_` twin of each one. The twin has the same columns, the same number of
rows and the same mess (mixed formats, blanks, junk values, odd encodings),
but none of the real SINs, names, addresses or other sensitive values.

### Which should I use?

| You want to... | Use |
|----------------|-----|
| Get a safe stand-in for a real file (or folder of files) to send to someone, test code against, or share, **with as little setup as possible** | [`fakegen synth`](#synthesize-twins-of-real-files-fakegen-synth) |
| Design a dataset from scratch, control exactly which values are allowed, or generate millions of rows from a spec | [`fakegen generate`](#quick-start) with a YAML schema |

## Table of contents

- [Prerequisites](#prerequisites)
- [Synthesize twins of real files (`fakegen synth`)](#synthesize-twins-of-real-files-fakegen-synth)
  - [5-minute tutorial](#5-minute-tutorial)
  - [A whole folder at once](#a-whole-folder-at-once)
  - [Reading the report](#reading-the-report)
  - [What gets detected, and how each type is faked](#what-gets-detected-and-how-each-type-is-faked)
  - [Fixing a wrong detection (overrides)](#fixing-a-wrong-detection-overrides)
  - [Very large files (millions of rows)](#very-large-files-millions-of-rows)
  - [All `synth` options](#all-synth-options)
  - [Using `synth` from Python](#using-synth-from-python)
  - [What `synth` guarantees, and what it doesn't](#what-synth-guarantees-and-what-it-doesnt)
  - [`synth` troubleshooting](#synth-troubleshooting)
- [Quick start (schema-driven `generate`)](#quick-start)
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

**New to Python / the terminal?** Every command in this README is typed
into a terminal (macOS: the *Terminal* app; Windows: *PowerShell*; or the
terminal panel in VS Code), after first moving into this project's folder:

```bash
cd path/to/create_fake_data     # the folder this README is in
uv sync                          # one-time: installs everything into .venv/
```

Every command starts with `uv run`, which runs it inside this project's own
Python environment, so there's nothing to "activate". To try the Python
examples interactively, start a Python prompt with `uv run python` and
paste them in (type `exit()` to leave).

## Synthesize twins of real files (`fakegen synth`)

`fakegen synth` reads a real file, **learns the shape of every column
without keeping its sensitive values**, and writes a synthetic twin next to
it called `synth_<original name>`:

```
real_data/clients.csv          →  real_data/synth_clients.csv
real_data/2024/visits.txt      →  real_data/2024/synth_visits.txt
real_data/payments.parquet     →  real_data/synth_payments.parquet
```

Supported inputs: delimited text (`.csv`, `.txt`, `.tsv`, `.dat`, `.psv`, with
any common delimiter and encoding) and Parquet (`.parquet`, `.pq`).

The twin keeps:

- the **same columns**, in the same order, with the same header;
- the **same number of rows** (unless you pass `--rows`);
- the **same file format**: encoding (UTF-8, Windows-1252, ...), delimiter,
  quoting style, line endings; Parquet column types are kept too;
- the **same mess**: if 35% of SINs are written `123 456 789`, 25% as
  `123-456-789`, 7% are blank, and a few say `N/A` or `see file`, the twin
  has roughly the same mix of *made-up* values. The same applies to
  upper/lower case, stray spaces, mixed date formats, and junk values that
  don't fit the column at all.

And it never copies across a real SIN, name, address, postal code, phone
number, e-mail, ID or free-text note (see
[the guarantees](#what-synth-guarantees-and-what-it-doesnt)).

### 5-minute tutorial

The repo ships with a folder of **entirely made-up but deliberately messy**
files in `examples/messy/` so you can try everything safely:

| File | What's messy about it |
|------|------------------------|
| `examples/messy/sample_clients.csv` | Semicolon-delimited, Windows-1252 encoding (accented names), Windows line endings; SINs in 4 formats plus blanks/`N/A`/junk; names in mixed case with trailing spaces; provinces written as `ON` / `Ontario` / `ont.`; postal codes with and without spaces; dates in 3 formats; `$1,234.56`-style amounts; free-text notes |
| `examples/messy/visits.txt` | Tab-delimited, every field quoted; SINs again; date-times; program codes including rare ones; staff names |
| `examples/messy/archive/payments.parquet` | Parquet with string, float, date and integer columns, and some nulls |

(They were made by `examples/make_messy_examples.py`; rerun it any time
with `uv run python examples/make_messy_examples.py`.)

**Step 1: do a dry run.** It reads the file and tells you what it found,
without writing anything:

```bash
uv run fakegen synth examples/messy/sample_clients.csv --dry-run
```

You'll get a table like this (abridged):

```
┏━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Column         ┃ Detected as    ┃ Blank % ┃ Junk % ┃ Formats ┃ Leak guard             ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━┩
│ client_id      │ identifier     │       0 │      0 │       1 │ clean                  │
│ SIN            │ sin            │       7 │    6.7 │       7 │ clean                  │
│ First Name     │ first_name     │       0 │      0 │       4 │ clean                  │
│ Email          │ email          │    42.7 │   17.7 │       3 │ clean                  │
│ Prov           │ province       │       0 │      0 │       4 │ n/a                    │
│ Postal Code    │ postal_code    │       0 │      0 │       7 │ clean                  │
│ DOB            │ date           │     4.3 │      3 │       4 │ n/a                    │
│ Status         │ category       │     7.3 │      0 │       0 │ 5 common value(s) kept │
│ Balance        │ numeric        │      42 │      0 │       4 │ n/a                    │
│ Notes          │ free_text      │    39.7 │      0 │       2 │ clean                  │
└────────────────┴────────────────┴─────────┴────────┴─────────┴────────────────────────┘
  cp1252, semicolon-delimited, minimal quoting, CRLF, header row · 300 rows in
```

Check the **Detected as** column. If something is wrong (say a column of
names came out as `identifier`), see [overrides](#fixing-a-wrong-detection-overrides).
Add `-v` to see *why* each column was detected the way it was.

**Step 2: generate the twin for real:**

```bash
uv run fakegen synth examples/messy/sample_clients.csv --seed 42
```

This writes `examples/messy/synth_sample_clients.csv`. (`--seed 42` makes
the output the same every time you run it; leave it out for different
random data each run.)

**Step 3: compare the two.** Open both files in VS Code or Excel, or look
at the first few rows in the terminal:

```bash
head -4 examples/messy/sample_clients.csv
head -4 examples/messy/synth_sample_clients.csv
```

Some columns from each (the real one is itself made-up example data):

```
ORIGINAL
SIN;First Name;LAST_NAME;Phone;Prov;Postal Code;DOB;Service Date
753645803;Élise;Chandler;call office;Alberta;T5J 9S4;08/07/2000;2021-05-12
;Éléonore;STEIN;353-134-3794;NS;b8j3p5;03/09/1950;2022-08-14
453853012;Jeanne;Cameron;9182850140;British Columbia;v7j 3j9;22/10/1997;14MAR2018

SYNTHETIC
SIN;First Name;LAST_NAME;Phone;Prov;Postal Code;DOB;Service Date
275162683;Pamela;Howard;hiyg oopohe;QC;j1g2n8;30JUL1968;07/09/2015
291285419;STEPHANIE;PERKINS;2868943235;Ontario;N5N4N6;1976-09-23;2015-10-22
127092563;EARL;MILLER;oxoz ppaxts;Quebec;j1r 5k1;2003-01-29;2016-04-15
```

What to notice:

- **SIN**: new 9-digit numbers that pass the SIN checksum, in the same mix
  of formats (`123 456 789`, `123-456-789`, `123456789`, blank, junk) as
  the original.
- **Phone**: junk like `call office` becomes junk of the same shape
  (`hiyg oopohe`), so code that must cope with bad phone numbers still gets
  tested.
- **Names**: new names with the original's casing habits (ALL CAPS, trailing
  spaces, ...).
- **Prov / Postal Code**: provinces written in the same mix of styles, and
  every postal code's first letter matches its row's province (`N` → Ontario,
  `J` → Quebec).
- **DOB / Service Date**: the same three date formats, similar date ranges,
  and a service date is never before the date of birth, as in the source.

### A whole folder at once

Point `synth` at a folder instead of a file:

```bash
uv run fakegen synth examples/messy                 # files directly in the folder
uv run fakegen synth examples/messy --recursive     # ...and every sub-folder (-r)
```

- Each supported file gets its own `synth_` twin **next to it**.
- Files that already start with the prefix (`synth_...`) are **skipped**,
  so running it twice doesn't create `synth_synth_...` files.
- Use `--out-dir` to keep the twins somewhere else. The folder structure is
  mirrored:

  ```bash
  uv run fakegen synth examples/messy -r --out-dir synthetic_out
  # synthetic_out/synth_sample_clients.csv
  # synthetic_out/synth_visits.txt
  # synthetic_out/archive/synth_payments.parquet
  ```
- Use `--prefix` to change the prefix, e.g. `--prefix fake_`.
- If one file fails (e.g. it's corrupt), the others are still processed; the
  failure is reported at the end and the command exits with an error code.

### Reading the report

After each file, `synth` prints one row per column:

| Report column | Meaning |
|---------------|---------|
| **Detected as** | The column's detected type (see [the next section](#what-gets-detected-and-how-each-type-is-faked)). Sensitive types are shown in magenta. |
| **Blank %** | Share of cells that were empty or a "blank-like" token (`N/A`, `NULL`, `-`, `unknown`, ...). The twin has about the same share of the same tokens. |
| **Junk %** | Share of non-blank cells that *don't* fit the detected type, e.g. `see file` in a SIN column. The twin has about the same share of made-up junk with the same character pattern. |
| **Formats** | How many different formats/patterns/casing styles were seen. A rough "messiness" score. |
| **Leak guard** | For sensitive columns: `clean` means no generated value happened to equal a real one; `N fixed` means N did and were replaced; `N unresolved` (red) means it couldn't find a safe replacement (see [troubleshooting](#synth-troubleshooting)). For categories: how many real values were common enough to be kept. `n/a` = not a sensitive type. |

Under the table: the detected file format, row counts and time taken, plus
lines like `(row names): 437 generated value(s) matched a real one and were
replaced`. That just means Faker's name list overlaps with names in your
data (normal for common names), and those overlapping names were never
used.

Save the full report (including the *why* for each column) as JSON with
`--report report.json`. The report never contains real values, so it's
safe to share.

### What gets detected, and how each type is faked

Detection uses the **column header** (e.g. `SIN_NO`, `Postal Code`,
`case_worker`) plus the **values** (e.g. 90% look like `A1A 1A1`), so
columns with unhelpful headers are usually still recognised.

| Type | Recognised by | How the synthetic value is made |
|------|---------------|---------------------------------|
| `sin` | 9 digits with optional ` `/`-`/`.` separators (checksum used when the header gives no hint) | Random 9-digit number with a valid SIN checksum (or invalid, as often as the source's were), poured into one of the source's formats |
| `first_name`, `last_name`, `full_name` | Header (`first_name`, `surname`, `client_name`, `worker`...) and alphabetic values; `full_name` also from values like `Julie Brooks` | Faker Canadian names, casing and layout copied (`Smith, John A.`, `JOHN SMITH`, ...). All name columns in a row describe the same fake person |
| `email` | Values look like e-mails | Built from the row's fake name; domains seen at least `k` times are kept, others become free-mail domains |
| `phone` | Header or `(613) 555-0199`-style values | Random digits in one of the source's patterns |
| `street_address`, `city` | Header, or `123 Main St`-style values | Faker Canadian street/city, casing copied |
| `province` | Values are Canadian provinces (`ON`, `Ontario`, `ont.`, `PQ`...) | Province drawn from the source's distribution, written in the source's style (abbreviation or full name) |
| `postal_code` | `A1A 1A1`-style values | Random valid-looking code **whose first letter matches the row's province**, in the source's layout (`k1a0b1`, `K1A 0B1`...) |
| `date` | Values parse as dates in ~20 formats (`2024-01-31`, `31/01/2024`, `31JAN2024`, `Jan 31, 2024`, with or without times) | Random date following the source's date distribution, in the source's mix of formats. Date columns that are always in order in the source (e.g. birth ≤ service) stay in order |
| `numeric` | Numbers, incl. `$1,234.50`, `-3`, `45%` | Random number following the source's distribution, in the source's formats (decimals, `$`, thousands separators) |
| `category` | ≤ 50 distinct values that repeat (status, region, program code...) | Values seen **at least `k` times** (default 5) are reused at their observed frequency; rarer values are replaced by random characters with the same pattern |
| `free_text` | Long, multi-word values, or a `notes`/`comments` header | Lorem-ipsum text of similar length and casing |
| `identifier` | Mostly-unique values, ID-like headers, or numbers with leading zeros | Random characters with the same pattern (`CL-000123` → `CL-000847`). Characters shared by at least `k` values (like the `CL-000` prefix) are kept |
| `unknown` | Nothing above fits | Random characters with the same pattern |
| `empty` | Every sampled value was blank | Blanks, in the same style |

`sin`, the name types, `email`, `phone`, `street_address`, `city`,
`postal_code`, `free_text`, `identifier` and `unknown` are the **sensitive**
types: every generated value in those columns is checked by the leak guard.

### Fixing a wrong detection (overrides)

If the dry run shows a column detected wrongly, create a small YAML file,
e.g. `overrides.yaml`:

```yaml
columns:            # applies to every file
  REF_NO: identifier
  Worker: full_name
  Comments: free_text

files:              # applies to one file only (matched by file name)
  visits.txt:
    program_code: category
```

and pass it in:

```bash
uv run fakegen synth real_data/ --overrides overrides.yaml --dry-run
```

Valid types are the ones in the table above: `sin`, `first_name`,
`last_name`, `full_name`, `email`, `phone`, `street_address`, `city`,
`province`, `postal_code`, `date`, `numeric`, `category`, `free_text`,
`identifier`, `unknown`, `empty`.

The safest choice when unsure is `identifier` or `unknown`: every
character is randomised and only the pattern survives. Be careful with
`category`, which reuses real values that occur at least `k` times, so
only use it for columns that aren't about individuals.

### Very large files (millions of rows)

For files with tens or hundreds of millions of rows you usually don't want
a full-size twin. Ask for a percentage instead:

```bash
uv run fakegen synth /data/extracts/claims_2024.csv --percent 1       # 1% of the rows
uv run fakegen synth /data/extracts/ -r --percent 0.1 --out-dir /data/synthetic
```

`--percent` works on any file (each twin is that percentage of *its own*
source, rounded up). For big files, fakegen also switches to **large-file
mode** automatically (for any file of 500 MB or more; force it with
`--large-file-mode on` or `off`). The normal mode reads every row in Python and keeps a fingerprint of
every real sensitive value in memory. That's exact and simple, but at
hundreds of millions of rows it would take hours and more memory than most
machines have. Large-file mode hands the heavy lifting to
[DuckDB](https://duckdb.org), which streams the file from disk using all
CPU cores:

| Step | Normal mode | Large-file mode |
|------|-------------|-----------------|
| Count rows | Python reads every row | DuckDB (Parquet: instant, from the file's metadata) |
| Learn the columns' shapes | The **first** `--sample-rows` rows | `--sample-rows` rows picked **from across the whole file** (better if the file is sorted, e.g. by date) |
| Leak guard | Every real value's fingerprint held in memory | Only the *generated* values are held; DuckDB streams the source once and reports which generated values match a real one. Those are replaced with spare values that were checked in the same pass |
| Memory use | Grows with the source | Grows with the output (a few GB at most for typical outputs) |

With `--percent`, the work done in Python (profiling and generating) grows
with the **output** size, not the source's. What's left in proportion to
the source is a few fast DuckDB passes.

**How long does it take?** On a laptop, a 909 MB / 5.1-million-row messy
semicolon-delimited Windows-1252 CSV with 16 columns, at `--percent 1`:

| Phase | Time |
|-------|------|
| Convert to a temporary UTF-8 copy (non-UTF-8 files only) | 7 s |
| Count rows + sample 200,000 rows from across the file | 12 s |
| Profile | 18 s |
| Generate 51,000 rows | 31 s |
| Leak check: every generated value vs. all 46 million real sensitive values | 90 s |
| **Total** | **2 min 40 s** (a full-size twin in normal mode would take an estimated 35–40 min) |

Roughly, the source-side phases take **~2 minutes per GB** of source, and
generation runs at about **1,500–2,500 rows per second** of output. So a
300-million-row file (~55 GB) at `--percent 1` would need roughly 2 hours
of source scanning plus about half an hour to generate 3 million rows.
At `--percent 0.1`, the generation part is only a few minutes.
(These are extrapolations from the benchmark above; your disk, CPU and
column mix will change them.)

**Things to know about large-file mode:**

- **Disk space:** scratch files go in a hidden folder next to the output
  and are deleted at the end. Budget roughly the source's size if it isn't
  UTF-8 (fakegen makes a one-off UTF-8 copy, because DuckDB can't read
  Windows-1252 directly), plus about 3× the output's size.
- The leak check has the same guarantee as normal mode (every real value
  in every sensitive column, all rows). The report shows how many values
  were replaced, e.g. `(row names): 2,326 generated value(s) matched a real
  one and were replaced`. That's expected with big files, since they contain
  most common first names.
- The same `--seed` gives the same output, as in normal mode. Normal and
  large-file mode learn from different samples, though, so their outputs
  for the same seed differ.
- Ragged-row warnings (rows with too few/many fields) are only produced in
  normal mode.

### All `synth` options

```bash
uv run fakegen synth --help
```

| Option | Default | What it does / when to use it |
|--------|---------|-------------------------------|
| `PATH` | *(required)* | A file, or a folder of files |
| `--out-dir DIR` | next to each source | Write twins under `DIR` instead (sub-folders mirrored) |
| `--prefix TEXT` | `synth_` | Output file-name prefix; files already starting with it are skipped in folder mode |
| `--recursive`, `-r` | off | Also process sub-folders |
| `--rows N` | same as source | Rows per output file, e.g. `--rows 100` for a small sample to email, or `--rows 1000000` for load testing |
| `--percent X` | `100` | Make each output X% of its own source's rows, e.g. `--percent 1` or `--percent 0.1` (rounded up; never 0 rows). Can't be combined with `--rows`. See [very large files](#very-large-files-millions-of-rows) |
| `--large-file-mode` | `auto` | `on` / `off` / `auto` (on for files of 500 MB or more). See [very large files](#very-large-files-millions-of-rows) |
| `--seed N` | random | Same seed + same input = identical output |
| `--locale` | `en_CA` | Faker locale for names/streets/cities (e.g. `fr_CA`) |
| `--k N` | `5` | A category value (or e-mail domain, or shared ID characters) is reused only if at least N rows share it. Raise it to be more cautious |
| `--category-threshold N` | `50` | Max distinct values for a column to count as a category |
| `--strict` | off | Fail the file (and delete its partial output) if any generated value can't be made different from every real value |
| `--sample-rows N` | `200000` | Rows used to *learn* each file's shape (`0` = all rows): the first N rows normally, or N rows picked from across the whole file in large-file mode. The leak guard always checks **every** row regardless |
| `--overrides FILE` | none | [Force column types](#fixing-a-wrong-detection-overrides) |
| `--report FILE` | none | Also save the report as JSON |
| `--dry-run` | off | Detect and report only; write nothing |
| `--verbose`, `-v` | off | Show why each column was detected as it was |
| `--encoding` | auto | Force the text encoding, e.g. `utf-8`, `cp1252`, `latin-1` |
| `--delimiter` | auto | Force the delimiter: a character, or `tab` / `comma` / `semicolon` / `pipe` |
| `--no-header` | off | The text files have no header row (columns are named `column_1`, `column_2`, ...) |

### Using `synth` from Python

```python
from fakegen import synthesize

# One file -> examples/messy/synth_sample_clients.csv
reports = synthesize("examples/messy/sample_clients.csv", seed=42)

# A whole folder, into another folder, with a few options
reports = synthesize(
    "examples/messy",
    "synthetic_out",               # out_dir (optional)
    recursive=True,
    percent=10,                    # each twin is 10% the size of its source (or rows=1_000)
    k=10,
    overrides={"columns": {"worker": "full_name"}},   # or a path to a YAML file
)

for report in reports:
    if not report.ok:
        print("FAILED", report.source, report.error)
        continue
    print(report.source, "->", report.output, f"({report.rows_out} rows)")
    for col in report.columns:
        print(f"   {col.name:<15} {col.semantic:<15} blank {col.null_pct}%  junk {col.junk_pct}%")
```

Every option in the table above is available as a keyword argument
(`dry_run=True`, `strict=True`, `sample_rows=None` for all rows,
`large_file=True` / `False` / `None` for on / off / auto, ...).
`report.to_dict()` gives the report as plain data (e.g. for `json.dump`).

### What `synth` guarantees, and what it doesn't

**Guaranteed:**

- **No real value from a sensitive column is written out.** Before writing,
  every value generated for a sensitive column is compared against *every*
  real value from *every* sensitive column of the source file (all rows,
  not just the sample). The comparison ignores case, spacing, punctuation
  and word order, so `123-456-789` counts as a match for `123 456 789`, and
  `SMITH, John` for `John Smith`. Anything that matches is regenerated. The
  only exceptions are values shorter than 3 characters (like `M`, `QC`,
  `12`), which can't identify anyone and are not checked. If a safe value
  truly can't be found, the report says `unresolved`, and `--strict` turns
  that into an error.
- **Real category values appear only if at least `k` rows share them**
  (k-anonymity): common values like `Active` or `EFT` are kept, while a
  value unique to one person is replaced.
- **Rows are not real people.** Every row's name, address, SIN, dates and
  so on are drawn independently of any real row, so no real record is
  reconstructed.
- **The profile is never saved.** Only masks (e.g. `999-999-999`), counts
  and distributions are kept in memory while the file is processed. The
  report contains no values.

**Not guaranteed. Please read before sharing output:**

- It is **not formal differential privacy**. Numbers and dates follow the
  real distributions, and minimum/maximum values are close to the real
  ones. That's usually fine, but if a single extreme value (e.g. one
  enormous salary) is itself sensitive, override that column to `unknown`.
- **Detection can be wrong.** A sensitive column with an unhelpful header
  and unusual values could be detected as `category`, in which case values
  shared by ≥ k rows are kept. **Always look at the dry-run report** and
  use overrides where needed.
- **Column headers are copied as-is**, so don't put sensitive data in headers.
- Relationships *between* files are not preserved. The same client gets
  different fake SINs in `clients.csv` and `visits.txt`, so joins across
  twins won't line up.
- Rows with too many or too few fields in the source are counted in the
  report but not reproduced: the twin is always rectangular.

### `synth` troubleshooting

**A column was detected as the wrong type**
Run with `--dry-run -v` to see why, then fix it with an
[overrides file](#fixing-a-wrong-detection-overrides).

**The output has garbled characters (`Ã©`, `�`), or everything is in one column**
The encoding or delimiter was mis-detected (rare, but possible with very
unusual files). Force it: `--encoding cp1252` or `--delimiter tab` (also
`comma`, `semicolon`, `pipe`, or any single character).

**The first data row went missing / the header shows data**
The file has no header row: add `--no-header`.

**"could not generate a value that differs from every real value" (with `--strict`)**
The column's possible values are too few to avoid every real one, e.g. a
3-digit code column where every combination appears in the real data. Use
`--overrides` to make it a `category` if it isn't about people, or run
without `--strict` and review the `unresolved` count.

**Lots of made-up words like `Tavoki` instead of real-looking names**
Every name Faker came up with was also in your data (possible with very
large files), so all of them were excluded and invented ones were used
instead. Try another locale (`--locale fr_CA`, `--locale en_GB`) for a
different name list.

**It's slow on a very big file**
Use `--percent` (e.g. `--percent 1`) so only a fraction of the rows is
generated, and check the report's last line says `large-file mode`. If it
doesn't (the file is under 500 MB), add `--large-file-mode on`. See
[very large files](#very-large-files-millions-of-rows) for what to expect.
Lowering `--sample-rows` (default 200,000) shortens the profiling step.

**"No space left on device" in large-file mode**
Scratch files are written next to the output. Point `--out-dir` at a disk
with more free space (see the disk-space note under
[very large files](#very-large-files-millions-of-rows)).

**A Parquet column comes out all-null**
Its type (e.g. list, struct, binary) isn't supported yet; the report
includes a warning naming the column.

## Quick start

This section is about the schema-driven `generate` mode. For automatic
twins of real files, see [`fakegen synth`](#synthesize-twins-of-real-files-fakegen-synth) above.

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
| `fakegen synth` | Writes a `synth_` twin of a real file or of every file in a folder. See [`fakegen synth`](#synthesize-twins-of-real-files-fakegen-synth). |

Useful `generate` flags: `--rows N` (override the row count without
editing the YAML), `--out-dir DIR`, `--formats csv parquet` (write both at
once), `--reference FILE` (for `passthrough` columns), `--chunk-size N`
(rows processed per batch — lower it if you're generating a very large
file on a memory-constrained machine; default 50,000).

## Privacy notes

(For `fakegen synth`, see [What `synth` guarantees, and what it doesn't](#what-synth-guarantees-and-what-it-doesnt).)

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

For `synthesize()` (the Python side of `fakegen synth`), see
[Using `synth` from Python](#using-synth-from-python).

## Project layout

```
create_fake_data/
├── pyproject.toml          # project metadata & dependencies (read by uv)
├── uv.lock                 # exact locked dependency versions
├── README.md               # this file
├── examples/
│   ├── example_schema.yaml
│   ├── example_columns.csv
│   ├── make_messy_examples.py  # (re)builds the messy tutorial files below
│   └── messy/                  # made-up but messy files for trying `synth`
├── src/fakegen/
│   ├── schema.py            # the config data model, YAML load/dump, validation
│   ├── generate.py          # the chunked generation engine (writes csv/parquet)
│   ├── infer.py             # builds a schema from a slice of a real file
│   ├── csv_schema.py        # CSV column-catalog -> YAML schema conversion
│   ├── providers.py         # validated Faker provider lookup
│   ├── cli.py                # the `fakegen` command-line tool
│   ├── synth/                # `fakegen synth`: twins of real files
│   │   ├── io.py             #   read/write CSV/TXT/Parquet, detect encoding & delimiter
│   │   ├── detect.py         #   work out each column's type (SIN, name, date, ...)
│   │   ├── profile.py        #   learn each column's formats/blanks/junk -- not its values
│   │   ├── render.py         #   masks ("999-999-999") and formatting helpers
│   │   ├── generators.py     #   build fake rows from the profiles
│   │   ├── guard.py          #   the leak guard (no real value gets out)
│   │   ├── scan.py           #   large-file mode: DuckDB counting, sampling and leak scanning
│   │   └── runner.py         #   file/folder handling, prefixes, reports
│   ├── __init__.py
│   └── __main__.py
└── tests/
```

## Running the tests

```bash
uv run pytest                              # everything
uv run pytest tests/test_synth_*.py -v     # just `synth`, one line per test
```

The `synth` tests build their own messy files in a temporary folder and
check, among other things, that **no real sensitive value appears anywhere
in the output**, that the mix of formats/blanks/junk is preserved, that
postal codes match provinces and dates stay in order, that rare categories
aren't copied, and that folder mode, `--out-dir`, `--rows`, `--seed` and
overrides behave as described above.

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
