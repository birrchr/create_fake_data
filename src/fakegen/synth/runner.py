"""
runner.py
~~~~~~~~~
Orchestrates `fakegen synth` for a single file or a whole directory:

  1. open the file and detect its encoding / delimiter / quoting / header
  2. profile up to `sample_rows` rows (see profile.py)
  3. load every real value of every sensitive column into the leak guard
  4. generate the same number of rows (or `rows`, or `percent` % of them) and
     write them next to the source (or under `out_dir`) as `<prefix><name>`
  5. return a FileReport per file -- which contains no real values

Files of LARGE_FILE_BYTES or more (or any file, with large_file=True) use
large-file mode instead: DuckDB counts and samples the source and does the
leak check by streaming it (see scan.py), so nothing proportional to the
source's size is ever held in Python memory. Combined with `percent`, the
work done in Python is proportional to the *output*, not the source.
"""

from __future__ import annotations

import bisect
import math
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

import yaml

from .detect import SEMANTIC_TYPES, SENSITIVE_TYPES
from .generators import Synthesizer
from .guard import GuardStats, LeakError, LeakGuard
from .io import SUPPORTED_SUFFIXES, SourceTable, open_source, open_writer
from .profile import build_profiles, is_null_token
from .render import fill_mask, mask_of

DEFAULT_PREFIX = "synth_"
DEFAULT_SAMPLE_ROWS = 200_000
DEFAULT_CHUNK_SIZE = 10_000
LARGE_FILE_BYTES = 500 * 1024 * 1024  # files at least this big use large-file mode by default
_SPARES = 2                           # spare values checked alongside each generated sensitive value
_ALTERNATIVES = 6                     # replacement candidates tried per leaked cell, per extra scan

ProgressCallback = Callable[[Path, int, int], None]  # (source, rows_done, rows_total)
StatusCallback = Callable[[Path, str], None]         # (source, what's happening now)


@dataclass
class ColumnReport:
    name: str
    semantic: str
    reason: str
    sensitive: bool
    null_pct: float
    junk_pct: float
    shapes: int
    kept_categories: int = 0
    collisions: int = 0
    fallbacks: int = 0
    unresolved: int = 0


@dataclass
class FileReport:
    source: str
    output: Optional[str] = None
    file_format: str = ""
    rows_in: int = 0
    rows_out: int = 0
    profiled_rows: int = 0
    columns: list[ColumnReport] = field(default_factory=list)
    row_level: dict[str, GuardStats] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None
    elapsed: float = 0.0
    dry_run: bool = False
    large_file: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict:
        return asdict(self)


# ─── overrides ────────────────────────────────────────────────────────────────

def load_overrides(overrides) -> dict:
    """
    Accepts a path to a YAML file, an already-loaded dict, or None. Format:

        columns:            # applies to every file
          SIN_NO: sin
        files:              # applies to one file (matched by file name)
          clients.csv:
            NOTES: free_text
    """
    if overrides is None:
        return {"columns": {}, "files": {}}
    if not isinstance(overrides, dict):
        with open(overrides, encoding="utf-8") as fh:
            overrides = yaml.safe_load(fh) or {}
    columns = dict(overrides.get("columns") or {})
    files = {name: dict(cols or {}) for name, cols in (overrides.get("files") or {}).items()}
    bad = [f"{col}: {sem}" for mapping in [columns, *files.values()] for col, sem in mapping.items()
           if sem not in SEMANTIC_TYPES]
    if bad:
        raise ValueError(
            "Unknown type(s) in overrides: " + ", ".join(bad)
            + f". Valid types: {', '.join(SEMANTIC_TYPES)}"
        )
    return {"columns": columns, "files": files}


def _overrides_for(overrides: dict, path: Path) -> dict[str, str]:
    return {**overrides["columns"], **overrides["files"].get(path.name, {})}


# ─── public entry points ──────────────────────────────────────────────────────

def find_source_files(root: Path, prefix: str = DEFAULT_PREFIX, recursive: bool = False) -> list[Path]:
    pattern = root.rglob("*") if recursive else root.glob("*")
    return sorted(
        p for p in pattern
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES and not p.name.startswith(prefix)
    )


def synthesize(
    path: str | Path,
    out_dir: Optional[str | Path] = None,
    *,
    prefix: str = DEFAULT_PREFIX,
    recursive: bool = False,
    on_file_start: Optional[Callable[[Path], None]] = None,
    **options,
) -> list[FileReport]:
    """
    Create a synthetic twin of one file, or of every supported file in a
    directory. Returns one FileReport per file. See `synthesize_file` for
    the other options.
    """
    path = Path(path)
    overrides = load_overrides(options.pop("overrides", None))
    if path.is_dir():
        sources = find_source_files(path, prefix, recursive)
        targets = [
            (src, (Path(out_dir) / src.relative_to(path).parent if out_dir else src.parent) / f"{prefix}{src.name}")
            for src in sources
        ]
    elif path.is_file():
        target_dir = Path(out_dir) if out_dir else path.parent
        targets = [(path, target_dir / f"{prefix}{path.name}")]
    else:
        raise FileNotFoundError(f"No such file or directory: {path}")

    reports = []
    for src, target in targets:
        if on_file_start:
            on_file_start(src)
        reports.append(synthesize_file(src, target, overrides=overrides, **options))
    return reports


def synthesize_file(
    src: str | Path,
    out_path: Optional[str | Path] = None,
    *,
    rows: Optional[int] = None,
    percent: Optional[float] = None,
    seed: Optional[int] = None,
    locale: str = "en_CA",
    k: int = 5,
    category_threshold: int = 50,
    strict: bool = False,
    sample_rows: Optional[int] = DEFAULT_SAMPLE_ROWS,
    overrides=None,
    dry_run: bool = False,
    encoding: Optional[str] = None,
    delimiter: Optional[str] = None,
    has_header: bool = True,
    large_file: Optional[bool] = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    on_progress: Optional[ProgressCallback] = None,
    on_status: Optional[StatusCallback] = None,
) -> FileReport:
    """
    Create a synthetic twin of one file. Errors are captured in the report, not raised.

    rows / percent -- output size: an exact row count, or a percentage of the
                      source's rows (e.g. 1 for 1%). Default: same as the source.
    large_file     -- True/False forces large-file mode on/off; None (default)
                      turns it on for files of LARGE_FILE_BYTES or more.
    """
    src = Path(src)
    out_path = Path(out_path) if out_path else src.with_name(f"{DEFAULT_PREFIX}{src.name}")
    report = FileReport(source=str(src), dry_run=dry_run)
    t0 = time.perf_counter()
    try:
        if rows is not None and percent is not None:
            raise ValueError("Give either rows or percent, not both.")
        if percent is not None and percent <= 0:
            raise ValueError(f"percent must be greater than 0 (got {percent}).")
        if out_path.resolve() == src.resolve():
            raise ValueError("Output path is the same as the source file -- refusing to overwrite it.")
        if large_file is None:
            large_file = src.stat().st_size >= LARGE_FILE_BYTES
        report.large_file = large_file
        run = _run_large if large_file else _run
        run(src, out_path, report, rows=rows, percent=percent, seed=seed, locale=locale, k=k,
            category_threshold=category_threshold, strict=strict, sample_rows=sample_rows,
            overrides=_overrides_for(load_overrides(overrides) if not _is_loaded(overrides) else overrides, src),
            dry_run=dry_run, encoding=encoding, delimiter=delimiter, has_header=has_header,
            chunk_size=chunk_size, on_progress=on_progress,
            on_status=(lambda msg: on_status(src, msg)) if on_status else (lambda msg: None))
    except Exception as exc:  # noqa: BLE001 -- one bad file must not stop a directory run
        report.error = f"{type(exc).__name__}: {exc}"
        report.output = None
        if not dry_run and out_path.exists() and out_path.resolve() != src.resolve():
            out_path.unlink()
    report.elapsed = time.perf_counter() - t0
    return report


def _is_loaded(overrides) -> bool:
    return isinstance(overrides, dict) and set(overrides) == {"columns", "files"}


def _rows_out(total: int, rows: Optional[int], percent: Optional[float]) -> int:
    if rows is not None:
        return rows
    if percent is not None:
        return max(1, math.ceil(total * percent / 100)) if total else 0
    return total


def _open_and_warn(src: Path, report: FileReport, overrides: dict, encoding, delimiter, has_header) -> SourceTable:
    table = open_source(src, encoding=encoding, delimiter=delimiter, has_header=has_header)
    report.file_format = table.describe()
    for name in table.unsupported_columns:
        report.warnings.append(f"Column '{name}' has a Parquet type fakegen can't synthesize; it is written as all-null.")
    unknown = sorted(set(overrides) - set(table.columns))
    if unknown:
        report.warnings.append(f"Overrides name column(s) not in this file: {', '.join(unknown)}")
    return table


# ─── normal mode: every row read in Python, leak guard held in memory ─────────

def _run(src: Path, out_path: Path, report: FileReport, *, rows, percent, seed, locale, k, category_threshold,
         strict, sample_rows, overrides, dry_run, encoding, delimiter, has_header, chunk_size, on_progress,
         on_status):
    table = _open_and_warn(src, report, overrides, encoding, delimiter, has_header)

    # 1. read a sample for profiling, and count every row
    on_status("reading")
    sample: list[list] = []
    total = 0
    for row in table.iter_rows():
        if sample_rows is None or total < sample_rows:
            sample.append(row)
        total += 1
    report.rows_in = total
    report.profiled_rows = len(sample)
    if table.ragged_rows:
        report.warnings.append(
            f"{table.ragged_rows} row(s) had a different number of fields than the header; "
            f"they were padded/truncated when profiling (the output is always rectangular)."
        )
    if table.blank_lines:
        report.warnings.append(f"{table.blank_lines} blank line(s) were skipped.")

    # 2. profile
    on_status("profiling")
    profiles, date_groups = build_profiles(table.columns, sample, k=k,
                                           category_threshold=category_threshold, overrides=overrides)

    # 3. leak guard: every real value in every sensitive column, across ALL rows
    on_status("loading the leak guard")
    guard = LeakGuard(strict=strict)
    sensitive = [p.index for p in profiles if p.semantic in SENSITIVE_TYPES]
    rows_for_guard = sample if total == len(sample) else table.iter_rows()
    for row in rows_for_guard:
        for i in sensitive:
            v = row[i]
            if not is_null_token(v):
                guard.add_real(v)
    del sample

    n_out = _rows_out(total, rows, percent)
    if not dry_run:
        # 4. generate + write in chunks
        on_status("generating")
        file_seed = None if seed is None else f"{seed}:{src.name}"
        synth = Synthesizer(profiles, date_groups, guard, seed=file_seed, locale=locale, rows=n_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        writer = open_writer(table, out_path)
        try:
            done = 0
            while done < n_out:
                n = min(chunk_size, n_out - done)
                writer.write_columns(synth.generate(n))
                done += n
                if on_progress:
                    on_progress(src, done, n_out)
        finally:
            writer.close()
        report.output = str(out_path)
        report.rows_out = n_out

    _fill_report(report, profiles, guard)


def _fill_report(report: FileReport, profiles, guard: LeakGuard) -> None:
    """Per-column report: shapes and rates only -- never values."""
    for p in profiles:
        st = guard.stats.get(p.name, GuardStats())
        report.columns.append(ColumnReport(
            name=p.name, semantic=p.semantic, reason=p.reason,
            sensitive=p.semantic in SENSITIVE_TYPES,
            null_pct=round(100 * p.null_rate, 1), junk_pct=round(100 * p.junk_rate, 1),
            shapes=p.n_shapes, kept_categories=len(p.categories),
            collisions=st.collisions, fallbacks=st.fallbacks, unresolved=st.unresolved,
        ))
    report.row_level = {label: st for label, st in guard.stats.items() if label.startswith("(row ")}


# ─── large-file mode: DuckDB streams the source, Python only touches the output ─

def _run_large(src: Path, out_path: Path, report: FileReport, *, rows, percent, seed, locale, k,
               category_threshold, strict, sample_rows, overrides, dry_run, encoding, delimiter, has_header,
               chunk_size, on_progress, on_status):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from .scan import DuckSource

    table = _open_and_warn(src, report, overrides, encoding, delimiter, has_header)
    # Scratch space (a UTF-8 copy of a non-UTF-8 source, the generated rows
    # before the leak check) lives next to the output, where there's room
    # for a file of that size; dry runs write nothing there.
    if not dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{out_path.name}.", dir=None if dry_run else out_path.parent))
    source = None
    try:
        source = DuckSource(table, tmp, on_status)
        # 1. count, and take a sample spread across the whole file
        on_status("counting rows")
        total = source.count()
        on_status("sampling rows from across the file")
        sample = source.sample(sample_rows, total, seed)
        report.rows_in = total
        report.profiled_rows = len(sample)

        # 2. profile
        on_status("profiling")
        profiles, date_groups = build_profiles(table.columns, sample, k=k,
                                               category_threshold=category_threshold, overrides=overrides)
        del sample

        # The in-memory guard stays empty: real values are checked by scanning
        # the source (step 4) rather than by loading them all into memory.
        guard = LeakGuard(strict=strict)
        n_out = _rows_out(total, rows, percent)
        if dry_run:
            _fill_report(report, profiles, guard)
            return

        ncols = len(table.columns)
        sensitive = [p.index for p in profiles if p.semantic in SENSITIVE_TYPES]
        file_seed = None if seed is None else f"{seed}:{src.name}"
        synth = Synthesizer(profiles, date_groups, guard, seed=file_seed, locale=locale, rows=n_out)
        cand_schema = pa.schema([("id", pa.int64()), ("v", pa.string())])
        rows_schema = pa.schema([(f"c{i}", pa.string()) for i in range(ncols)])
        rows_path, cand_path = tmp / "rows.parquet", tmp / "candidates.parquet"

        # 3. generate into a temporary file, noting every sensitive cell as a
        #    "candidate" to check. Each cell (row * ncols + column) also gets
        #    _SPARES spare values checked in the same scan, so a second scan is
        #    only needed if a cell's value AND all its spares match real ones.
        #    Candidate id = cell * (_SPARES + 1) + j, where j = 0 is the value
        #    actually written. Pool entries (first/last names, cities) are
        #    candidates too, with ids < 0; values drawn from a pool need no
        #    spares, as a filtered pool is itself a safe source of replacements.
        on_status("generating")
        stride = _SPARES + 1
        spare_cols = {i for i in sensitive if not synth.columns[i].pool_rendered}
        pools = synth.factory.warm_pools()
        pool_index = [(kind, i) for kind, values in pools.items() for i in range(len(values))]
        with pq.ParquetWriter(rows_path, rows_schema) as rows_writer, \
                pq.ParquetWriter(cand_path, cand_schema) as cand_writer:
            if pool_index:
                cand_writer.write_table(pa.table({
                    "id": pa.array([-1 - j for j in range(len(pool_index))], pa.int64()),
                    "v": pa.array([pools[kind][i] for kind, i in pool_index], pa.string()),
                }, schema=cand_schema))
            done = 0
            while done < n_out:
                n = min(chunk_size, n_out - done)
                cols = synth.generate(n)
                rows_writer.write_table(pa.table(
                    [pa.array([None if v is None else str(v) for v in c], pa.string()) for c in cols],
                    schema=rows_schema))
                ids, vals = [], []
                for i in sensitive:
                    gen = synth.columns[i]
                    for r, v in enumerate(cols[i]):
                        if v is not None and not is_null_token(v):
                            base = ((done + r) * ncols + i) * stride
                            ids.append(base)
                            vals.append(str(v))
                            if i in spare_cols:
                                for j in range(1, stride):
                                    ids.append(base + j)
                                    vals.append(str(gen.fresh()))
                cand_writer.write_table(pa.table(
                    {"id": pa.array(ids, pa.int64()), "v": pa.array(vals, pa.string())}, schema=cand_schema))
                done += n
                if on_progress:
                    on_progress(src, done, n_out)

        # 4. leak check: each round is one streaming pass over the source
        on_status("checking every generated value against every real value (scan 1)")
        leaked = source.find_leaks(cand_path, sensitive)
        dropped: dict[str, set[int]] = {}
        for cid in leaked:
            if cid < 0:
                kind, i = pool_index[-1 - cid]
                dropped.setdefault(kind, set()).add(i)
        for kind, idx in dropped.items():
            synth.factory.drop_from_pool(kind, idx)

        # A leaked cell takes a value from its now-filtered pool (names,
        # cities), or its first spare that didn't leak. Only if every spare
        # leaked too does it get fresh alternatives for another scan.
        spares: dict[int, list[str]] = {}
        if any(c >= 0 and c % stride == 0 for c in leaked):
            leaked_cells = {c // stride for c in leaked if c >= 0 and c % stride == 0}
            for batch in pq.ParquetFile(cand_path).iter_batches(batch_size=1_000_000):
                for cid, v in zip(batch.column("id").to_pylist(), batch.column("v").to_pylist()):
                    if cid >= 0 and cid % stride and cid // stride in leaked_cells and cid not in leaked:
                        spares.setdefault(cid // stride, []).append(v)
        pending: dict[int, list[str]] = {}
        replacements: dict[int, str] = {}
        for cell in sorted(c // stride for c in leaked if c >= 0 and c % stride == 0):
            gen = synth.columns[cell % ncols]
            guard.record(gen.p.name, collisions=1)
            safe = gen.pool_replacement()
            if safe is not None:
                replacements[cell] = safe
            elif spares.get(cell):
                replacements[cell] = spares[cell][0]
            else:
                pending[cell] = [str(gen.fresh()) for _ in range(_ALTERNATIVES)]

        for scan_no in (2, 3):
            if not pending:
                break
            on_status(f"re-checking replacements for {len(pending):,} value(s) (scan {scan_no})")
            alt_path = tmp / f"alternatives_{scan_no}.parquet"
            pq.write_table(pa.table({
                "id": pa.array([cid * _ALTERNATIVES + j for cid, alts in pending.items() for j in range(len(alts))],
                               pa.int64()),
                "v": pa.array([a for alts in pending.values() for a in alts], pa.string()),
            }, schema=cand_schema), alt_path)
            bad = source.find_leaks(alt_path, sensitive)
            still: dict[int, list[str]] = {}
            for cid, alts in pending.items():
                good = [a for j, a in enumerate(alts) if cid * _ALTERNATIVES + j not in bad]
                if good:
                    replacements[cid] = good[0]
                else:
                    # Every alternative matched too: try random characters of the same shape.
                    guard.stats.setdefault(synth.columns[cid % ncols].p.name, GuardStats()).fallbacks += 1
                    still[cid] = [fill_mask(mask_of(alts[0]), synth.rng) for _ in range(_ALTERNATIVES)]
            pending = still

        for cid, alts in pending.items():
            name = synth.columns[cid % ncols].p.name
            guard.stats.setdefault(name, GuardStats()).unresolved += 1
            if strict:
                raise LeakError(
                    f"Column '{name}': could not generate a value that differs from every real value "
                    f"(the column's value space is too small). Re-run without --strict, or override "
                    f"this column's type."
                )
            replacements[cid] = alts[0]

        # 5. write the final file in the source's format, applying replacements
        on_status("writing")
        replaced_ids = sorted(replacements)
        writer = open_writer(table, out_path)
        try:
            row0 = 0
            for batch in pq.ParquetFile(rows_path).iter_batches(batch_size=chunk_size):
                cols = [c.to_pylist() for c in batch.columns]
                lo = bisect.bisect_left(replaced_ids, row0 * ncols)
                hi = bisect.bisect_left(replaced_ids, (row0 + batch.num_rows) * ncols)
                for cid in replaced_ids[lo:hi]:
                    r, i = divmod(cid, ncols)
                    cols[i][r - row0] = replacements[cid]
                writer.write_columns(cols)
                row0 += batch.num_rows
        finally:
            writer.close()
        report.output = str(out_path)
        report.rows_out = n_out
        _fill_report(report, profiles, guard)
    finally:
        if source is not None:
            source.close()
        shutil.rmtree(tmp, ignore_errors=True)
