"""
scan.py
~~~~~~~
Large-file mode: DuckDB-powered access to source files too big to walk
through row by row in Python (hundreds of millions of rows).

In normal mode, fakegen reads every source row in Python (to count rows and
to load every real sensitive value into the in-memory leak guard). That
doesn't scale to hundreds of millions of rows -- it would take hours and
more memory than the machine has. In large-file mode DuckDB, which is
multi-threaded and streams from disk, does the heavy lifting instead:

  count()       -- number of rows (instant for Parquet: read from metadata)
  sample()      -- a random sample spread over the WHOLE file (not just the
                   first rows), chosen by a hash of each row so it is the
                   same every run
  find_leaks()  -- the leak guard turned around: rather than holding every
                   real value in memory, the (much smaller) set of generated
                   values is held, and DuckDB streams the source once to find
                   which generated values equal a real one

Values are compared with the same normalised "keys" in SQL on both sides
(case-, spacing-, punctuation- and word-order-insensitive), so a generated
"123-456-789" is caught if "123 456 789" is in the source.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import duckdb
import pyarrow.parquet as pq

from .io import SourceTable, _stringifier
from .profile import NULL_TOKENS

_MAX_LINE = 64 * 1024 * 1024


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _ident(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


_NULL_TOKENS_SQL = ", ".join(_sql_str(t) for t in sorted(NULL_TOKENS))


def keys_sql(values_sql: str, id_col: Optional[str] = None) -> str:
    """
    Turn a query producing a column `v` (and optionally an id column) into
    one producing (id, k): up to two hashed keys per non-blank value.
      '#' + letters/digits only, lower-cased   ("123 456-789" -> "#123456789")
      '~' + words sorted, lower-cased          ("Smith, John" -> "~john smith")
    Values with fewer than 3 letters/digits are skipped, as in guard.py.
    """
    id_sel = f"{id_col}, " if id_col else ""
    return f"""
        SELECT {id_sel}k FROM (
            SELECT {id_sel}unnest([
                CASE WHEN length(a) >= 3 THEN hash('#' || a) END,
                CASE WHEN len(t) > 1 THEN hash('~' || array_to_string(list_sort(t), ' ')) END
            ]) AS k
            FROM (
                SELECT {id_sel}t, array_to_string(t, '') AS a
                FROM (
                    SELECT {id_sel}regexp_extract_all(lower(v), '[\\pL\\pN]+') AS t
                    FROM ({values_sql})
                    WHERE v IS NOT NULL AND trim(v) NOT IN ({_NULL_TOKENS_SQL})
                )
            )
        ) WHERE k IS NOT NULL
    """


class DuckSource:
    """
    A source file opened through DuckDB. Columns are exposed positionally as c0, c1, ...

    DuckDB reads text files as UTF-8 (fast) or Latin-1 (slower, and it rejects
    bytes 0x80-0x9F, which Windows-1252 files use for curly quotes, dashes and
    letters like Š). So a text file that isn't UTF-8 -- or turns out to have
    invalid UTF-8 part-way through -- is first converted, once, to a temporary
    UTF-8 copy in `work_dir`, decoded exactly as normal mode decodes it.
    """

    def __init__(self, table: SourceTable, work_dir: Path, on_status: Callable[[str], None] = lambda m: None):
        self.table = table
        self.ncols = len(table.columns)
        self.work_dir = work_dir
        self.on_status = on_status
        self.con = duckdb.connect()
        self.con.execute("SET enable_progress_bar = false")  # fakegen shows its own progress
        self.con.execute(f"SET temp_directory = {_sql_str(str(work_dir / 'duckdb'))}")  # spill here if RAM runs short
        self.path = table.path
        if table.kind == "text" and not table.text_format.encoding.startswith("utf"):
            self._convert_to_utf8()
        self.relation = self._relation()

    def close(self) -> None:
        self.con.close()

    def _convert_to_utf8(self) -> None:
        self.on_status(f"converting from {self.table.text_format.encoding} to a temporary UTF-8 copy")
        out = self.work_dir / "source-utf8.txt"
        with open(self.table.path, encoding=self.table.text_format.encoding, errors="replace", newline="") as fin, \
                open(out, "w", encoding="utf-8", newline="") as fout:
            while chunk := fin.read(16 * 1024 * 1024):
                fout.write(chunk)
        self.path = out

    def _relation(self) -> str:
        path = _sql_str(str(self.path))
        if self.table.kind == "parquet":
            cols = ", ".join(f"{_ident(name)} AS c{i}" for i, name in enumerate(self.table.columns))
            return f"(SELECT {cols} FROM read_parquet({path}))"
        fmt = self.table.text_format
        columns = "{" + ", ".join(f"'c{i}': 'VARCHAR'" for i in range(self.ncols)) + "}"
        return (
            f"read_csv({path}, delim={_sql_str(fmt.delimiter)}, quote={_sql_str(fmt.quotechar)}, "
            f"escape={_sql_str(fmt.quotechar)}, header=false, skip={1 if fmt.has_header else 0}, "
            f"auto_detect=false, columns={columns}, encoding='utf-8', strict_mode=false, "
            f"null_padding=true, max_line_size={_MAX_LINE})"
        )

    # --- rows -----------------------------------------------------------------

    def count(self) -> int:
        if self.table.kind == "parquet":
            return pq.ParquetFile(self.table.path).metadata.num_rows
        try:
            return self.con.execute(f"SELECT count(*) FROM {self.relation}").fetchone()[0]
        except duckdb.InvalidInputException as exc:
            if "encoded" not in str(exc) or self.path != self.table.path:
                raise
            # Looked like UTF-8 at the start, but isn't all the way through.
            self._convert_to_utf8()
            self.relation = self._relation()
            return self.con.execute(f"SELECT count(*) FROM {self.relation}").fetchone()[0]

    def sample(self, n: Optional[int], total: int, seed: Optional[int]) -> list[list[Optional[str]]]:
        """About `n` rows picked evenly at random across the whole file (all rows if n is None)."""
        cols = ", ".join(f"c{i}" for i in range(self.ncols))
        salt = int(seed) if isinstance(seed, int) else 0
        hashed = f"SELECT *, hash({cols}, {salt}) AS _h FROM {self.relation}"
        if n is None or n >= total:
            query = f"SELECT {cols} FROM ({hashed}) ORDER BY _h"
        else:
            # Keep rows whose hash falls in the lowest n/total of the hash range
            # (with a little headroom), so the pick is spread over the whole file
            # and identical every run no matter how DuckDB parallelises the read.
            cutoff = min(2**64 - 1, int(2**64 * min(1.0, 1.1 * n / total)))
            query = f"SELECT {cols} FROM ({hashed}) WHERE _h < {cutoff} ORDER BY _h LIMIT {int(n)}"
        if self.table.kind == "parquet":
            arrow = self.con.execute(query).to_arrow_table()
            converters = [_stringifier(f.type) for f in arrow.schema]
            columns = [[None if v is None else conv(v) for v in col.to_pylist()]
                       for conv, col in zip(converters, arrow.columns)]
            return [list(r) for r in zip(*columns)]
        # A delimited file has no real nulls: DuckDB's NULL is an empty field.
        return [["" if v is None else v for v in r] for r in self.con.execute(query).fetchall()]

    # --- leak checking --------------------------------------------------------

    def _real_values_sql(self, sensitive: list[int]) -> str:
        values = ", ".join(f"CAST(c{i} AS VARCHAR)" for i in sensitive)
        return f"SELECT unnest([{values}]) AS v FROM {self.relation}"

    def find_leaks(self, candidates: Path, sensitive: list[int]) -> set[int]:
        """
        `candidates` is a Parquet file of (id BIGINT, v VARCHAR) generated values.
        Returns the ids of those that match a real value in any sensitive column.
        One streaming pass over the source; memory use is proportional to the
        number of candidates, not the size of the source.
        """
        if not sensitive:
            return set()
        con = self.con
        con.execute(f"CREATE OR REPLACE TEMP TABLE cand_keys AS "
                    f"{keys_sql(f'SELECT id, v FROM read_parquet({_sql_str(str(candidates))})', 'id')}")
        con.execute(f"CREATE OR REPLACE TEMP TABLE hit_keys AS SELECT DISTINCT k FROM "
                    f"({keys_sql(self._real_values_sql(sensitive))}) WHERE k IN (SELECT k FROM cand_keys)")
        ids = {r[0] for r in con.execute(
            "SELECT DISTINCT id FROM cand_keys WHERE k IN (SELECT k FROM hit_keys)").fetchall()}
        con.execute("DROP TABLE cand_keys")
        con.execute("DROP TABLE hit_keys")
        return ids
