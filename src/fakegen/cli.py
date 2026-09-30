"""
cli.py
~~~~~~
Entry point for the `fakegen` tool.

Modes
-----
init        — scaffold an example schema (and example columns CSV) to start from
infer       — build a starter schema from an exact slice of a real reference file
csv-to-yaml — convert a spreadsheet-style column catalog (CSV) into a schema
validate    — check a schema file for errors without generating anything
generate    — generate the dataset(s) a schema describes
synth       — create `synth_` twins of real (messy, sensitive) files, one file or a whole folder

Usage
-----
    uv run fakegen init
    uv run fakegen infer --source real.parquet --columns site_id treatment_arm age --rows 2000
    uv run fakegen csv-to-yaml --csv columns.csv --out schema.yaml --rows 5000
    uv run fakegen validate --schema schema.yaml
    uv run fakegen generate --schema schema.yaml --out-dir data
    uv run fakegen synth examples/messy --recursive
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _check_deps():
    missing = []
    for pkg in ("faker", "duckdb", "pandas", "pyarrow", "yaml", "rich"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(
            f"[ERROR] Missing dependencies: {', '.join(missing)}\n"
            "Run:  uv sync\n"
            "Then: uv run fakegen ...",
            file=sys.stderr,
        )
        sys.exit(1)


_check_deps()

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

console = Console()


# ─── init ─────────────────────────────────────────────────────────────────────

def cmd_init(args: argparse.Namespace) -> None:
    from ._examples import EXAMPLE_COLUMNS_CSV, EXAMPLE_SCHEMA_YAML

    schema_path = Path(args.out)
    csv_path = Path(args.csv_out)

    for path, content, label in (
        (schema_path, EXAMPLE_SCHEMA_YAML, "schema"),
        (csv_path, EXAMPLE_COLUMNS_CSV, "columns CSV"),
    ):
        if path.exists() and not args.force:
            console.print(f"[yellow]Skipping[/yellow] {label} — {path} already exists (use --force to overwrite)")
            continue
        path.write_text(content, encoding="utf-8")
        console.print(f"[green]✅ Wrote[/green] {label} to {path}")

    console.print(
        "\nNext steps:\n"
        f"  uv run fakegen validate --schema {schema_path}\n"
        f"  uv run fakegen generate --schema {schema_path} --out-dir data"
    )


# ─── infer ────────────────────────────────────────────────────────────────────

def cmd_infer(args: argparse.Namespace) -> None:
    from .infer import infer_schema, write_inferred_schema

    source_path = Path(args.source)
    if not source_path.exists():
        console.print(f"[red]✗ File not found:[/red] {source_path}")
        sys.exit(1)

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="bold cyan")
    table.add_column("Value", style="white")
    table.add_row("Source", str(source_path))
    table.add_row("Columns", ", ".join(args.columns) if args.columns else "(auto-detecting all columns)")
    table.add_row("Rows sampled", f"{args.rows:,} (offset {args.row_offset:,})")
    table.add_row("Category threshold", str(args.threshold))
    console.print(Panel(table, title="[bold]fakegen infer[/bold]", border_style="blue"))

    spec, notes = infer_schema(
        source=str(source_path),
        columns=args.columns,
        rows=args.rows,
        row_offset=args.row_offset,
        threshold=args.threshold,
        weighted=args.weighted,
    )
    resolved_columns = [c.name for c in spec.columns]
    write_inferred_schema(spec, notes, str(source_path), resolved_columns, args.out)

    if not args.columns:
        console.print(f"[dim]Auto-detected {len(resolved_columns)} column(s): {', '.join(resolved_columns)}[/dim]")
    console.print(f"\n[green]✅ Wrote inferred schema to:[/green] {args.out}")
    if notes:
        console.print(f"\n[yellow]⚠️  {len(notes)} column(s) need manual attention:[/yellow]")
        for n in notes:
            console.print(f"  - {n}")
        console.print(
            "\nOpen the schema file and replace those `needs_mapping` entries "
            "before running `generate`."
        )
    else:
        console.print("[green]All columns were inferred with a safe strategy.[/green]")


# ─── csv-to-yaml ────────────────────────────────────────────────────────────────

def cmd_csv_to_yaml(args: argparse.Namespace) -> None:
    from .csv_schema import csv_to_yaml

    csv_path = Path(args.csv)
    if not csv_path.exists():
        console.print(f"[red]✗ File not found:[/red] {csv_path}")
        sys.exit(1)

    csv_to_yaml(
        csv_path=csv_path,
        out_path=args.out,
        rows=args.rows,
        seed=args.seed,
        formats=args.formats,
        output_dir=args.output_dir,
        file_stem=args.file_stem,
        reference=args.reference,
    )
    console.print(f"[green]✅ Wrote schema to:[/green] {args.out}")


# ─── validate ─────────────────────────────────────────────────────────────────

def cmd_validate(args: argparse.Namespace) -> None:
    from .schema import load_schema, validate_schema

    spec = load_schema(args.schema)
    errors = validate_schema(spec)

    if not errors:
        console.print(f"[green bold]✅ {args.schema} is valid[/green bold] — {len(spec.columns)} column(s), {spec.rows:,} rows.")
        return

    console.print(f"[red bold]✗ {len(errors)} problem(s) in {args.schema}:[/red bold]")
    for e in errors:
        console.print(f"  - {e}")
    sys.exit(1)


# ─── generate ─────────────────────────────────────────────────────────────────

def cmd_generate(args: argparse.Namespace) -> None:
    from .generate import generate_dataset
    from .schema import SchemaError, load_schema, validate_schema

    schema_path = Path(args.schema)
    if not schema_path.exists():
        console.print(f"[red]✗ File not found:[/red] {schema_path}")
        sys.exit(1)

    spec = load_schema(schema_path)
    errors = validate_schema(spec)
    if errors:
        console.print(f"[red bold]✗ Schema is invalid — fix these first:[/red bold]")
        for e in errors:
            console.print(f"  - {e}")
        sys.exit(1)

    total_rows = args.rows if args.rows is not None else spec.rows

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="bold cyan")
    table.add_column("Value", style="white")
    table.add_row("Schema", str(schema_path))
    table.add_row("Rows", f"{total_rows:,}")
    table.add_row("Columns", str(len(spec.columns)))
    table.add_row("Formats", ", ".join(args.formats or spec.formats))
    table.add_row("Output dir", str(args.out_dir or spec.output_dir))
    if args.reference or spec.reference:
        table.add_row("Reference", str(args.reference or spec.reference))
    console.print(Panel(table, title="[bold]fakegen generate[/bold]", border_style="blue"))

    if args.formats:
        spec.formats = args.formats

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed:,}/{task.total:,} rows"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Generating ...", total=total_rows)

        def on_chunk(done: int, total: int) -> None:
            progress.update(task, completed=done)

        try:
            stats = generate_dataset(
                spec,
                out_dir=args.out_dir,
                rows=args.rows,
                reference=args.reference,
                reference_limit=args.reference_limit,
                chunk_size=args.chunk_size,
                on_chunk=on_chunk,
            )
        except SchemaError as exc:
            progress.stop()
            console.print(f"\n[red bold]✗ {exc}[/red bold]")
            sys.exit(1)

    console.print(f"\n[green bold]✅ Done[/green bold] in {stats['elapsed']:.2f}s")
    for f in stats["files"]:
        size_mb = Path(f).stat().st_size / 1e6
        console.print(f"  {f}  ({size_mb:.1f} MB)")


# ─── synth ────────────────────────────────────────────────────────────────────

_DELIMITER_ALIASES = {"tab": "\t", "\\t": "\t", "comma": ",", "semicolon": ";", "pipe": "|"}


def cmd_synth(args: argparse.Namespace) -> None:
    import json

    from .synth import synthesize

    path = Path(args.path)
    if not path.exists():
        console.print(f"[red]✗ Not found:[/red] {path}")
        sys.exit(1)

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="bold cyan")
    table.add_column("Value", style="white")
    table.add_row("Source", f"{path}{' (folder' + (', recursive)' if args.recursive else ')') if path.is_dir() else ''}")
    table.add_row("Output", str(args.out_dir) if args.out_dir else "next to each source file")
    table.add_row("Prefix", args.prefix)
    if args.rows is not None:
        table.add_row("Rows", f"{args.rows:,} per file")
    elif args.percent is not None:
        table.add_row("Rows", f"{args.percent:g}% of each source file")
    else:
        table.add_row("Rows", "same as each source file")
    table.add_row("Locale / seed", f"{args.locale} / {args.seed if args.seed is not None else 'random'}")
    table.add_row("k (category privacy)", str(args.k))
    table.add_row("Large-file mode", {"auto": "auto (files ≥ 500 MB)", "on": "on", "off": "off"}[args.large_file_mode])
    if args.overrides:
        table.add_row("Overrides", args.overrides)
    if args.dry_run:
        table.add_row("Mode", "[yellow]dry run — profile and report only, nothing written[/yellow]")
    console.print(Panel(table, title="[bold]fakegen synth[/bold]", border_style="blue"))

    delimiter = _DELIMITER_ALIASES.get(args.delimiter, args.delimiter) if args.delimiter else None

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed:,}/{task.total:,} rows"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        tasks: dict = {}

        def on_file_start(src: Path) -> None:
            tasks[src] = progress.add_task(f"{src.name} ...", total=0)

        def on_progress(src: Path, done: int, total: int) -> None:
            progress.update(tasks[src], completed=done, total=total)

        def on_status(src: Path, message: str) -> None:
            progress.update(tasks[src], description=f"{src.name}: {message} ...")

        try:
            reports = synthesize(
                path, args.out_dir, prefix=args.prefix, recursive=args.recursive,
                rows=args.rows, percent=args.percent, seed=args.seed, locale=args.locale, k=args.k,
                category_threshold=args.category_threshold, strict=args.strict,
                sample_rows=args.sample_rows or None, overrides=args.overrides,
                dry_run=args.dry_run, encoding=args.encoding, delimiter=delimiter,
                has_header=not args.no_header,
                large_file={"auto": None, "on": True, "off": False}[args.large_file_mode],
                on_file_start=on_file_start, on_progress=on_progress, on_status=on_status,
            )
        except (ValueError, OSError) as exc:
            progress.stop()
            console.print(f"[red bold]✗ {exc}[/red bold]")
            sys.exit(1)

    if not reports:
        console.print("[yellow]No supported files found[/yellow] (.csv .txt .tsv .dat .psv .parquet .pq, "
                      f"not already starting with '{args.prefix}').")
        return

    for r in reports:
        _print_file_report(r, verbose=args.verbose)

    ok = [r for r in reports if r.ok]
    failed = [r for r in reports if not r.ok]
    console.print(
        f"\n[bold]{len(ok)} file(s) {'profiled' if args.dry_run else 'synthesized'}[/bold]"
        + (f", [red bold]{len(failed)} failed[/red bold]" if failed else "")
    )
    if args.report:
        Path(args.report).write_text(json.dumps([r.to_dict() for r in reports], indent=2), encoding="utf-8")
        console.print(f"Report written to {args.report}")
    if failed:
        sys.exit(1)


def _print_file_report(r, verbose: bool = False) -> None:
    if not r.ok:
        console.print(f"\n[red bold]✗ {r.source}[/red bold]\n  {r.error}")
        return
    title = f"{r.source}  →  {r.output}" if r.output else f"{r.source}  (dry run)"
    table = Table(title=title, title_justify="left", title_style="bold green", header_style="bold cyan")
    table.add_column("Column")
    table.add_column("Detected as")
    table.add_column("Blank %", justify="right")
    table.add_column("Junk %", justify="right")
    table.add_column("Formats", justify="right")
    table.add_column("Leak guard")
    if verbose:
        table.add_column("Why")
    for c in r.columns:
        if not c.sensitive:
            guard = "[dim]n/a[/dim]" if c.semantic != "category" else f"[dim]{c.kept_categories} common value(s) kept[/dim]"
        elif c.unresolved:
            guard = f"[red]{c.unresolved} unresolved[/red]"
        elif c.collisions:
            guard = f"[yellow]{c.collisions} fixed[/yellow]"
        else:
            guard = "[green]clean[/green]"
        semantic = f"[magenta]{c.semantic}[/magenta]" if c.sensitive else c.semantic
        cells = [c.name, semantic, f"{c.null_pct:g}", f"{c.junk_pct:g}", str(c.shapes), guard]
        if verbose:
            cells.append(c.reason)
        table.add_row(*cells)
    console.print()
    console.print(table)
    rows = f"{r.rows_in:,} rows in" + (f", {r.rows_out:,} rows out" if r.output else "")
    if r.profiled_rows < r.rows_in:
        which = "a random" if r.large_file else "the first"
        rows += f" (profiled {which} {r.profiled_rows:,})"
    mode = " · large-file mode" if r.large_file else ""
    console.print(f"  [dim]{r.file_format} · {rows} · {r.elapsed:.1f}s{mode}[/dim]")
    for label, st in r.row_level.items():
        if st.collisions:
            console.print(f"  [dim]{label}: {st.collisions} generated value(s) matched a real one and were replaced"
                          + (f", {st.fallbacks} via fallback" if st.fallbacks else "") + "[/dim]")
    for w in r.warnings:
        console.print(f"  [yellow]⚠ {w}[/yellow]")


# ─── Parser ───────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="fakegen",
        description="fakegen — YAML/CSV-driven synthetic data generation, powered by Faker + DuckDB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = root.add_subparsers(dest="command", required=True)

    # init
    p = sub.add_parser("init", help="Scaffold an example schema to start from")
    p.add_argument("--out", default="schema.yaml", metavar="FILE")
    p.add_argument("--csv-out", default="columns.csv", metavar="FILE")
    p.add_argument("--force", action="store_true", help="Overwrite existing files")
    p.set_defaults(func=cmd_init)

    # infer
    p = sub.add_parser("infer", help="Build a starter schema from a slice of a real reference file")
    p.add_argument("--source", required=True, metavar="FILE", help="Reference .parquet or .csv file")
    p.add_argument("--columns", nargs="+", default=None, metavar="COL",
                   help="Exact columns to read. Omit to auto-detect every column in --source.")
    p.add_argument("--rows", type=int, required=True, metavar="N", help="Exact number of rows to read")
    p.add_argument("--row-offset", type=int, default=0, metavar="N", help="Skip this many rows before reading (default: 0)")
    p.add_argument("--threshold", type=int, default=50, metavar="N",
                   help="Max distinct values for a text column to be treated as a category (default: 50)")
    p.add_argument("--weighted", action="store_true",
                   help="Weight inferred code_set values by their observed frequency (default: uniform)")
    p.add_argument("--out", default="schema.yaml", metavar="FILE")
    p.set_defaults(func=cmd_infer)

    # csv-to-yaml
    p = sub.add_parser("csv-to-yaml", help="Convert a column-catalog CSV into a schema YAML")
    p.add_argument("--csv", required=True, metavar="FILE")
    p.add_argument("--out", default="schema.yaml", metavar="FILE")
    p.add_argument("--rows", type=int, default=1000, metavar="N")
    p.add_argument("--seed", type=int, default=None, metavar="N")
    p.add_argument("--formats", nargs="+", default=["csv"], choices=["csv", "parquet"])
    p.add_argument("--output-dir", default="data", metavar="DIR")
    p.add_argument("--file-stem", default="synthetic", metavar="NAME")
    p.add_argument("--reference", default=None, metavar="FILE",
                   help="Reference file, if any column uses type 'passthrough'")
    p.set_defaults(func=cmd_csv_to_yaml)

    # validate
    p = sub.add_parser("validate", help="Check a schema file without generating data")
    p.add_argument("--schema", required=True, metavar="FILE")
    p.set_defaults(func=cmd_validate)

    # generate
    p = sub.add_parser("generate", help="Generate the dataset(s) a schema describes")
    p.add_argument("--schema", required=True, metavar="FILE")
    p.add_argument("--rows", type=int, default=None, metavar="N", help="Override dataset.rows")
    p.add_argument("--out-dir", default=None, metavar="DIR", help="Override dataset.output_dir")
    p.add_argument("--formats", nargs="+", default=None, choices=["csv", "parquet"], help="Override dataset.formats")
    p.add_argument("--reference", default=None, metavar="FILE", help="Reference file for 'passthrough' columns")
    p.add_argument("--reference-limit", type=int, default=200_000, metavar="N",
                   help="Max reference rows to load per passthrough column (default: 200,000)")
    p.add_argument("--chunk-size", type=int, default=50_000, metavar="N",
                   help="Rows generated/written per batch (default: 50,000)")
    p.set_defaults(func=cmd_generate)

    # synth
    p = sub.add_parser(
        "synth",
        help="Create synthetic twins of real CSV/TXT/Parquet files (one file or a whole folder)",
        description="Profile each real file and write a synthetic twin named <prefix><name> "
                    "that mirrors its formats and mess, with no real sensitive values carried over.",
    )
    p.add_argument("path", metavar="PATH", help="A file, or a folder of files")
    p.add_argument("--out-dir", default=None, metavar="DIR",
                   help="Write outputs here (mirroring sub-folders) instead of next to each source")
    p.add_argument("--prefix", default="synth_", help="Output file-name prefix (default: synth_)")
    p.add_argument("--recursive", "-r", action="store_true", help="Also process sub-folders")
    size = p.add_mutually_exclusive_group()
    size.add_argument("--rows", type=int, default=None, metavar="N",
                      help="Rows per output file (default: same as the source)")
    size.add_argument("--percent", type=float, default=None, metavar="X",
                      help="Make each output X%% of its source's row count, e.g. 1 or 0.1 (default: 100)")
    p.add_argument("--seed", type=int, default=None, metavar="N", help="Make output reproducible")
    p.add_argument("--locale", default="en_CA", help="Faker locale for names/addresses (default: en_CA)")
    p.add_argument("--k", type=int, default=5, metavar="N",
                   help="A category value is copied only if it appears at least N times (default: 5)")
    p.add_argument("--category-threshold", type=int, default=50, metavar="N",
                   help="Max distinct values for a column to count as a category (default: 50)")
    p.add_argument("--strict", action="store_true",
                   help="Fail a file if any generated value can't be made different from every real value")
    p.add_argument("--sample-rows", type=int, default=200_000, metavar="N",
                   help="Rows read to learn each file's shape; 0 = all (default: 200,000). "
                        "The leak guard always checks every row.")
    p.add_argument("--overrides", default=None, metavar="FILE",
                   help="YAML file forcing column types when detection gets one wrong")
    p.add_argument("--report", default=None, metavar="FILE", help="Also save the report as JSON")
    p.add_argument("--dry-run", action="store_true", help="Profile and report only; write nothing")
    p.add_argument("--verbose", "-v", action="store_true", help="Show why each column was detected as it was")
    p.add_argument("--encoding", default=None, help="Force the text encoding (default: auto-detect)")
    p.add_argument("--delimiter", default=None,
                   help="Force the delimiter: a character, or tab/comma/semicolon/pipe (default: auto-detect)")
    p.add_argument("--no-header", action="store_true", help="Text files have no header row")
    p.add_argument("--large-file-mode", choices=["auto", "on", "off"], default="auto",
                   help="Let DuckDB count, sample and leak-check the source by streaming it, for files too big "
                        "to read in Python (default: auto = on for files of 500 MB or more)")
    p.set_defaults(func=cmd_synth)

    return root


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
