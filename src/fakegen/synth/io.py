"""
io.py
~~~~~
Reading and writing source files for `fakegen synth`.

Delimited text (CSV/TXT/TSV/...) is read with Python's csv module and every
cell is kept as the exact string in the file -- leading zeros, stray spaces,
"N/A" and all -- because that mess is what we want to mirror. The file's
encoding, delimiter, quoting style, line endings and header are detected and
reused when the synthetic twin is written.

Parquet is read with pyarrow in batches; every value is turned into a string
for profiling, and turned back into the column's original type on writing.
"""

from __future__ import annotations

import codecs
import csv
import datetime as dt
import decimal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import pyarrow as pa
import pyarrow.parquet as pq

TEXT_SUFFIXES = {".csv", ".txt", ".tsv", ".dat", ".psv"}
PARQUET_SUFFIXES = {".parquet", ".pq"}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | PARQUET_SUFFIXES
DELIMITERS = [",", ";", "\t", "|"]
_DELIMITER_NAMES = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


@dataclass
class TextFormat:
    encoding: str
    delimiter: str
    quotechar: str = '"'
    quoting: int = csv.QUOTE_MINIMAL
    lineterminator: str = "\n"
    has_header: bool = True

    def describe(self) -> str:
        delim = _DELIMITER_NAMES.get(self.delimiter, repr(self.delimiter))
        quoting = "all fields quoted" if self.quoting == csv.QUOTE_ALL else "minimal quoting"
        eol = "CRLF" if self.lineterminator == "\r\n" else "LF"
        header = "header row" if self.has_header else "no header"
        return f"{self.encoding}, {delim}-delimited, {quoting}, {eol}, {header}"


class SourceTable:
    """A readable source file: its columns plus a way to stream its rows as strings."""

    def __init__(self, path: Path, columns: list[str], text_format: Optional[TextFormat] = None,
                 arrow_schema: Optional[pa.Schema] = None):
        self.path = path
        self.columns = columns
        self.text_format = text_format
        self.arrow_schema = arrow_schema
        self.ragged_rows = 0
        self.blank_lines = 0
        self.unsupported_columns: list[str] = []

    @property
    def kind(self) -> str:
        return "parquet" if self.arrow_schema is not None else "text"

    def describe(self) -> str:
        if self.text_format:
            return self.text_format.describe()
        return "parquet"

    def iter_rows(self) -> Iterator[list[Optional[str]]]:
        if self.kind == "parquet":
            yield from self._iter_parquet()
        else:
            yield from self._iter_text()

    def _iter_text(self) -> Iterator[list[Optional[str]]]:
        fmt = self.text_format
        ncols = len(self.columns)
        self.ragged_rows = self.blank_lines = 0
        with open(self.path, encoding=fmt.encoding, errors="replace", newline="") as fh:
            reader = csv.reader(fh, delimiter=fmt.delimiter, quotechar=fmt.quotechar)
            header_skipped = not fmt.has_header
            for row in reader:
                if not row:
                    self.blank_lines += 1
                    continue
                if not header_skipped:
                    header_skipped = True
                    continue
                if len(row) != ncols:
                    self.ragged_rows += 1
                    row = (row + [""] * ncols)[:ncols]
                yield row

    def _iter_parquet(self) -> Iterator[list[Optional[str]]]:
        pf = pq.ParquetFile(self.path)
        converters = [_stringifier(f.type) for f in self.arrow_schema]
        for batch in pf.iter_batches(batch_size=50_000):
            cols = []
            for conv, arr in zip(converters, batch.columns):
                cols.append([None if v is None else conv(v) for v in arr.to_pylist()])
            yield from (list(r) for r in zip(*cols))


# ─── opening ──────────────────────────────────────────────────────────────────

def open_source(
    path: str | Path,
    encoding: Optional[str] = None,
    delimiter: Optional[str] = None,
    has_header: bool = True,
) -> SourceTable:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in PARQUET_SUFFIXES:
        schema = pq.read_schema(path)
        table = SourceTable(path, list(schema.names), arrow_schema=schema)
        table.unsupported_columns = [f.name for f in schema if _stringifier(f.type) is _unsupported]
        return table
    if suffix not in TEXT_SUFFIXES:
        raise ValueError(f"Unsupported file type: {path.name} (supported: {', '.join(sorted(SUPPORTED_SUFFIXES))})")

    enc = encoding or sniff_encoding(path)
    with open(path, encoding=enc, errors="replace", newline="") as fh:
        sample = fh.read(256 * 1024)
    fmt = sniff_text_format(sample, enc, path.suffix.lower(), delimiter, has_header)

    with open(path, encoding=enc, errors="replace", newline="") as fh:
        first = next((r for r in csv.reader(fh, delimiter=fmt.delimiter, quotechar=fmt.quotechar) if r), [])
    if has_header:
        columns = first
    else:
        columns = [f"column_{i + 1}" for i in range(len(first))]
    if not columns:
        raise ValueError(f"{path.name} is empty.")
    return SourceTable(path, columns, text_format=fmt)


def sniff_encoding(path: Path) -> str:
    with open(path, "rb") as fh:
        head = fh.read(4 * 1024 * 1024)
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    for enc in ("utf-8", "cp1252"):
        try:
            codecs.getincrementaldecoder(enc)().decode(head, final=False)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def sniff_text_format(sample: str, encoding: str, suffix: str,
                      delimiter: Optional[str], has_header: bool) -> TextFormat:
    lines = [ln for ln in sample.splitlines() if ln.strip()][:50]
    if len(sample) >= 256 * 1024 and len(lines) > 1:
        lines = lines[:-1]  # the last line may have been cut off mid-way
    if delimiter is None:
        delimiter = _guess_delimiter(lines, suffix)
    quoting = csv.QUOTE_MINIMAL
    data_lines = lines[1:21] if has_header else lines[:20]
    if data_lines and all(ln.startswith('"') and ln.rstrip().endswith('"') for ln in data_lines):
        quoting = csv.QUOTE_ALL
    lineterminator = "\r\n" if "\r\n" in sample else "\n"
    return TextFormat(encoding=encoding, delimiter=delimiter, quoting=quoting,
                      lineterminator=lineterminator, has_header=has_header)


def _guess_delimiter(lines: list[str], suffix: str) -> str:
    if not lines:
        return "\t" if suffix == ".tsv" else ","
    best, best_score = None, None
    for d in DELIMITERS:
        counts = [len(r) for r in csv.reader(lines, delimiter=d)]
        mode = max(set(counts), key=counts.count)
        if mode < 2:
            continue
        score = (counts.count(mode) / len(counts), mode)
        if best_score is None or score > best_score:
            best, best_score = d, score
    if best is None:
        return "\t" if suffix == ".tsv" else ","
    return best


# ─── parquet value conversion ─────────────────────────────────────────────────

def _unsupported(v):
    return None


def _stringifier(t: pa.DataType):
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        return str
    if pa.types.is_boolean(t) or pa.types.is_integer(t) or pa.types.is_floating(t) or pa.types.is_decimal(t):
        return str
    if pa.types.is_date(t):
        return lambda v: v.strftime("%Y-%m-%d")
    if pa.types.is_timestamp(t):
        return lambda v: v.strftime("%Y-%m-%d %H:%M:%S")
    return _unsupported


def _to_arrow(values: list, t: pa.DataType) -> pa.Array:
    def conv(v):
        if v is None:
            return None
        s = str(v).strip()
        if pa.types.is_string(t) or pa.types.is_large_string(t):
            return str(v)
        if pa.types.is_boolean(t):
            return {"true": True, "false": False, "1": True, "0": False}.get(s.lower())
        cleaned = s.replace(",", "").replace("$", "").rstrip("%")
        if pa.types.is_integer(t):
            return int(round(float(cleaned)))
        if pa.types.is_floating(t):
            return float(cleaned)
        if pa.types.is_decimal(t):
            return decimal.Decimal(cleaned).quantize(decimal.Decimal(1).scaleb(-t.scale))
        if pa.types.is_date(t):
            return dt.date.fromisoformat(s[:10])
        if pa.types.is_timestamp(t):
            return dt.datetime.fromisoformat(s)
        return None

    out = []
    for v in values:
        try:
            out.append(conv(v))
        except (ValueError, ArithmeticError):
            out.append(None)
    try:
        return pa.array(out, type=t)
    except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError):
        return pa.nulls(len(values), type=t)


# ─── writers ──────────────────────────────────────────────────────────────────

class TextWriter:
    def __init__(self, path: Path, fmt: TextFormat, columns: list[str]):
        self.path = path
        self._fh = open(path, "w", encoding=fmt.encoding, errors="replace", newline="")
        self._writer = csv.writer(
            self._fh, delimiter=fmt.delimiter, quotechar=fmt.quotechar,
            quoting=fmt.quoting, lineterminator=fmt.lineterminator,
        )
        if fmt.has_header:
            self._writer.writerow(columns)

    def write_columns(self, cols: list[list]) -> None:
        for row in zip(*cols):
            self._writer.writerow(["" if v is None else v for v in row])

    def close(self) -> None:
        self._fh.close()


class ParquetWriter:
    def __init__(self, path: Path, schema: pa.Schema):
        self.path = path
        self._schema = schema.remove_metadata()
        self._writer = pq.ParquetWriter(str(path), self._schema)

    def write_columns(self, cols: list[list]) -> None:
        arrays = [_to_arrow(c, f.type) for c, f in zip(cols, self._schema)]
        self._writer.write_table(pa.Table.from_arrays(arrays, schema=self._schema))

    def close(self) -> None:
        self._writer.close()


def open_writer(table: SourceTable, out_path: Path):
    if table.kind == "parquet":
        return ParquetWriter(out_path, table.arrow_schema)
    return TextWriter(out_path, table.text_format, table.columns)
