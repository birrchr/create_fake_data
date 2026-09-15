"""
cli.py
~~~~~~
Entry point for the `fakegen` tool.

Four modes
----------
init        — scaffold an example schema (and example columns CSV) to start from
infer       — build a starter schema from an exact slice of a real reference file
csv-to-yaml — convert a spreadsheet-style column catalog (CSV) into a schema
validate    — check a schema file for errors without generating anything
generate    — generate the dataset(s) a schema describes

Usage
-----
    uv run fakegen init
    uv run fakegen infer --source real.parquet --columns site_id treatment_arm age --rows 2000
    uv run fakegen csv-to-yaml --csv columns.csv --out schema.yaml --rows 5000
    uv run fakegen validate --schema schema.yaml
    uv run fakegen generate --schema schema.yaml --out-dir data
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

    return root


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
