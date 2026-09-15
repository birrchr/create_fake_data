import csv

from fakegen.csv_schema import csv_to_yaml
from fakegen.schema import load_schema, validate_schema


def _write_csv(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_csv_to_yaml_round_trip(tmp_path):
    csv_path = tmp_path / "columns.csv"
    fieldnames = ["name", "type", "provider", "kwargs_json", "values", "weights", "missing_pct"]
    _write_csv(csv_path, [
        {"name": "id", "type": "sequence", "provider": "", "kwargs_json": "", "values": "", "weights": "", "missing_pct": ""},
        {"name": "age", "type": "faker", "provider": "random_int", "kwargs_json": '{"min": 18, "max": 90}', "values": "", "weights": "", "missing_pct": ""},
        {"name": "arm", "type": "code_set", "provider": "", "kwargs_json": "", "values": "A|B|C", "weights": "0.5|0.25|0.25", "missing_pct": "10"},
    ], fieldnames)

    out_path = tmp_path / "schema.yaml"
    spec = csv_to_yaml(csv_path, out_path, rows=500)

    assert validate_schema(spec) == []

    reloaded = load_schema(out_path)
    by_name = {c.name: c for c in reloaded.columns}
    assert by_name["id"].type == "sequence"
    assert by_name["age"].params["kwargs"] == {"min": 18, "max": 90}
    assert by_name["arm"].params["values"] == ["A", "B", "C"]
    assert by_name["arm"].params["weights"] == [0.5, 0.25, 0.25]
    assert by_name["arm"].missing_pct == 10.0
