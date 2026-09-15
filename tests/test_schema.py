from fakegen.schema import ColumnSpec, DatasetSpec, dump_schema, load_schema, validate_schema


def test_round_trips_through_yaml(tmp_path):
    spec = DatasetSpec(
        rows=10,
        seed=1,
        columns=[
            ColumnSpec(name="id", type="sequence", params={"start": 1}),
            ColumnSpec(name="name", type="faker", params={"provider": "name"}),
        ],
    )
    path = tmp_path / "schema.yaml"
    dump_schema(spec, path)
    loaded = load_schema(path)

    assert loaded.rows == 10
    assert loaded.seed == 1
    assert [c.name for c in loaded.columns] == ["id", "name"]
    assert loaded.columns[0].params["start"] == 1


def test_validate_flags_needs_mapping():
    spec = DatasetSpec(rows=5, columns=[ColumnSpec(name="x", type="needs_mapping")])
    errors = validate_schema(spec)
    assert any("needs_mapping" in e or "needs your attention" in e.lower() or "placeholder" in e for e in errors)


def test_validate_flags_missing_code_set_values():
    spec = DatasetSpec(rows=5, columns=[ColumnSpec(name="x", type="code_set")])
    errors = validate_schema(spec)
    assert any("code_set" in e for e in errors)


def test_validate_flags_passthrough_without_reference():
    spec = DatasetSpec(
        rows=5,
        columns=[ColumnSpec(name="x", type="passthrough", params={"source_column": "y"})],
    )
    errors = validate_schema(spec)
    assert any("reference" in e for e in errors)


def test_validate_passes_for_reasonable_schema():
    spec = DatasetSpec(
        rows=5,
        columns=[
            ColumnSpec(name="id", type="sequence"),
            ColumnSpec(name="arm", type="code_set", params={"values": ["A", "B"]}),
        ],
    )
    assert validate_schema(spec) == []
