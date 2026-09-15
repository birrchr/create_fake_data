import pandas as pd

from fakegen.infer import discover_columns, infer_schema


def test_infers_numeric_category_and_high_cardinality_columns(tmp_path):
    df = pd.DataFrame({
        "age": [20, 25, 30, 35, 40, 45, 50, 55],
        "region": ["North", "South", "North", "South", "East", "East", "North", "South"],
        "free_text_id": [f"person-{i}-{'x' * i}" for i in range(8)],
    })
    src = tmp_path / "ref.parquet"
    df.to_parquet(src)

    spec, notes = infer_schema(
        source=str(src),
        columns=["age", "region", "free_text_id"],
        rows=8,
        threshold=3,
    )

    by_name = {c.name: c for c in spec.columns}
    assert by_name["age"].type == "numeric_range"
    assert by_name["age"].params["min"] == 20
    assert by_name["age"].params["max"] == 55

    assert by_name["region"].type == "code_set"
    assert set(by_name["region"].params["values"]) == {"North", "South", "East"}

    # 8 distinct free-text values > threshold of 3 -> flagged, not copied.
    assert by_name["free_text_id"].type == "needs_mapping"
    assert any("free_text_id" in n for n in notes)


def test_only_requested_row_window_is_read(tmp_path):
    df = pd.DataFrame({"v": list(range(100))})
    src = tmp_path / "ref.csv"
    df.to_csv(src, index=False)

    spec, _ = infer_schema(source=str(src), columns=["v"], rows=10, row_offset=90)
    v_col = spec.columns[0]

    assert v_col.params["min"] == 90
    assert v_col.params["max"] == 99


def test_columns_none_auto_detects_every_column(tmp_path):
    df = pd.DataFrame({
        "age": [20, 25, 30, 35, 40],
        "region": ["North", "South", "North", "South", "East"],
    })
    src = tmp_path / "ref.parquet"
    df.to_parquet(src)

    assert discover_columns(str(src)) == ["age", "region"]

    spec, _ = infer_schema(source=str(src), rows=5, threshold=3)

    assert [c.name for c in spec.columns] == ["age", "region"]
    assert spec.columns[0].type == "numeric_range"
    assert spec.columns[1].type == "code_set"
