import csv

import pytest

from fakegen.generate import generate_dataset
from fakegen.schema import ColumnSpec, DatasetSpec, SchemaError


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_generates_exact_row_and_column_count(tmp_path):
    spec = DatasetSpec(
        rows=25,
        seed=1,
        formats=["csv"],
        output_dir=str(tmp_path),
        columns=[
            ColumnSpec(name="id", type="sequence"),
            ColumnSpec(name="arm", type="code_set", params={"values": ["A", "B", "C"]}),
        ],
    )
    stats = generate_dataset(spec, chunk_size=10)
    rows = _read_csv(tmp_path / "synthetic.csv")

    assert stats["rows"] == 25
    assert len(rows) == 25
    assert list(rows[0].keys()) == ["id", "arm"]
    assert all(r["arm"] in ("A", "B", "C") for r in rows)


def test_sequence_and_constant(tmp_path):
    spec = DatasetSpec(
        rows=5,
        formats=["csv"],
        output_dir=str(tmp_path),
        columns=[
            ColumnSpec(name="id", type="sequence", params={"start": 100, "step": 2}),
            ColumnSpec(name="flag", type="constant", params={"value": "X"}),
        ],
    )
    generate_dataset(spec)
    rows = _read_csv(tmp_path / "synthetic.csv")

    assert [r["id"] for r in rows] == ["100", "102", "104", "106", "108"]
    assert all(r["flag"] == "X" for r in rows)


def test_missing_pct_produces_blanks(tmp_path):
    spec = DatasetSpec(
        rows=200,
        seed=7,
        formats=["csv"],
        output_dir=str(tmp_path),
        columns=[ColumnSpec(name="v", type="constant", params={"value": "x"}, missing_pct=50.0)],
    )
    generate_dataset(spec)
    rows = _read_csv(tmp_path / "synthetic.csv")
    n_blank = sum(1 for r in rows if r["v"] == "")

    assert 40 < n_blank < 160  # roughly half, allowing for randomness


def test_chunking_gives_same_row_count_regardless_of_chunk_size(tmp_path):
    def build(chunk_size):
        out = tmp_path / f"c{chunk_size}"
        spec = DatasetSpec(
            rows=37,
            seed=3,
            formats=["csv"],
            output_dir=str(out),
            columns=[ColumnSpec(name="id", type="sequence")],
        )
        generate_dataset(spec, chunk_size=chunk_size)
        return _read_csv(out / "synthetic.csv")

    assert len(build(5)) == len(build(1000)) == 37


def test_invalid_schema_raises_before_writing_anything(tmp_path):
    spec = DatasetSpec(
        rows=5,
        output_dir=str(tmp_path),
        columns=[ColumnSpec(name="x", type="needs_mapping")],
    )
    with pytest.raises(SchemaError):
        generate_dataset(spec)
    assert not (tmp_path / "synthetic.csv").exists()


def test_parquet_output(tmp_path):
    pd = pytest.importorskip("pandas")
    spec = DatasetSpec(
        rows=10,
        formats=["parquet"],
        output_dir=str(tmp_path),
        columns=[ColumnSpec(name="id", type="sequence")],
    )
    generate_dataset(spec)
    df = pd.read_parquet(tmp_path / "synthetic.parquet")
    assert len(df) == 10
    assert list(df["id"]) == list(range(1, 11))
