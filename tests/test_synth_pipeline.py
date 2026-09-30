import csv
import datetime as dt
import random
import re

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from faker import Faker

from fakegen import synthesize
from fakegen.synth import synthesize_file
from fakegen.synth.detect import POSTAL_FIRST_LETTERS, PROVINCE_LOOKUP
from fakegen.synth.guard import LeakGuard

SENSITIVE_COLUMNS = ["SIN", "First Name", "LAST_NAME", "Email", "Street", "Postal Code", "Notes"]
N = 200


def _messy_rows(n=N, seed=7):
    rng = random.Random(seed)
    fake = Faker("en_CA")
    fake.seed_instance(seed)
    rows = []
    for i in range(n):
        sin = re.sub(r"\D", "", fake.ssn())
        prov = rng.choice(["ON", "QC", "BC"])
        postal = rng.choice(POSTAL_FIRST_LETTERS[prov]) + "1A 2B3"
        dob = fake.date_of_birth(minimum_age=20, maximum_age=80)
        rows.append({
            "SIN": rng.choice([f"{sin[:3]} {sin[3:6]} {sin[6:]}", f"{sin[:3]}-{sin[3:6]}-{sin[6:]}", sin, "N/A", "", "see file"]),
            "First Name": rng.choice([fake.first_name(), fake.first_name().upper() + " "]),
            "LAST_NAME": fake.last_name().upper(),
            "Email": rng.choice([fake.email(), "", "none"]),
            "Street": fake.street_address(),
            "Province": rng.choice([prov, {"ON": "Ontario", "QC": "Quebec", "BC": "British Columbia"}[prov]]),
            "Postal Code": rng.choice([postal, postal.replace(" ", "").lower()]),
            "DOB": rng.choice([dob.isoformat(), dob.strftime("%d%b%Y").upper()]),
            "Start Date": (dob + dt.timedelta(days=rng.randint(7000, 9000))).isoformat(),
            "Status": rng.choice(["Active"] * 8 + ["Closed"] * 4 + [f"odd-{i}"]),
            "Amount": rng.choice([f"${rng.uniform(1, 9999):,.2f}", ""]),
            "Notes": rng.choice(["", fake.sentence(nb_words=10)]),
        })
    return rows


@pytest.fixture
def messy_csv(tmp_path):
    rows = _messy_rows()
    path = tmp_path / "clients.csv"
    with open(path, "w", encoding="cp1252", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter=";", lineterminator="\r\n")
        w.writeheader()
        w.writerows(rows)
    return path, rows


def _read(path, encoding="cp1252", delimiter=";"):
    with open(path, encoding=encoding, newline="") as fh:
        return list(csv.DictReader(fh, delimiter=delimiter))


def test_single_file_mirrors_format_and_row_count(messy_csv):
    src, rows = messy_csv
    report = synthesize_file(src, src.with_name("synth_clients.csv"), seed=1)
    assert report.ok, report.error
    out = src.with_name("synth_clients.csv")
    raw = out.read_bytes()
    assert b"\r\n" in raw and raw.split(b"\r\n")[0].count(b";") == len(rows[0]) - 1
    synth = _read(out)
    assert len(synth) == len(rows)
    assert list(synth[0]) == list(rows[0])
    by_col = {c.name: c for c in report.columns}
    assert by_col["SIN"].semantic == "sin"
    assert by_col["Postal Code"].semantic == "postal_code"
    assert by_col["DOB"].semantic == "date"
    assert by_col["Status"].semantic == "category"


@pytest.mark.parametrize("large_file", [False, True], ids=["normal", "large-file-mode"])
def test_no_real_sensitive_value_survives(messy_csv, large_file):
    src, rows = messy_csv
    report = synthesize_file(src, src.with_name("synth_clients.csv"), seed=2, large_file=large_file)
    assert report.ok, report.error
    assert report.large_file == large_file
    synth = _read(src.with_name("synth_clients.csv"))
    assert len(synth) == len(rows)

    guard = LeakGuard()
    for r in rows:
        for col in SENSITIVE_COLUMNS:
            if r[col].strip() and r[col].strip() not in {"N/A", "none"}:
                guard.add_real(r[col])
    leaks = [(col, r[col]) for r in synth for col in SENSITIVE_COLUMNS
             if r[col].strip() not in {"", "N/A", "none"} and guard.is_leak(r[col])]
    assert leaks == []


def test_mess_is_mirrored(messy_csv):
    src, rows = messy_csv
    synthesize_file(src, src.with_name("synth_clients.csv"), seed=3)
    synth = _read(src.with_name("synth_clients.csv"))

    def share(rs, col, pred):
        return sum(pred(r[col]) for r in rs) / len(rs)

    for pred in (
        lambda v: v == "",
        lambda v: v == "N/A",
        lambda v: bool(re.fullmatch(r"\d{3} \d{3} \d{3}", v)),
        lambda v: bool(re.fullmatch(r"\d{3}-\d{3}-\d{3}", v)),
    ):
        assert abs(share(rows, "SIN", pred) - share(synth, "SIN", pred)) < 0.12
    # Upper-cased first names with a trailing space are reproduced too.
    assert any(v.endswith(" ") and v.strip().isupper() for v in (r["First Name"] for r in synth))
    # All-caps DATE9-style dates ("05MAR1980") appear alongside ISO dates.
    assert any(re.fullmatch(r"\d{2}[A-Z]{3}\d{4}", r["DOB"]) for r in synth)
    assert any(re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["DOB"]) for r in synth)


def test_rows_are_coherent(messy_csv):
    src, _ = messy_csv
    synthesize_file(src, src.with_name("synth_clients.csv"), seed=4)
    synth = _read(src.with_name("synth_clients.csv"))
    for r in synth:
        abbr = PROVINCE_LOOKUP[r["Province"].strip().rstrip(".").casefold()]
        assert r["Postal Code"][0].upper() in POSTAL_FIRST_LETTERS[abbr]
        start = dt.date.fromisoformat(r["Start Date"])
        dob = r["DOB"]
        dob_date = (dt.date.fromisoformat(dob) if "-" in dob else dt.datetime.strptime(dob, "%d%b%Y").date())
        assert dob_date <= start


def test_rare_categories_are_not_copied(messy_csv):
    src, rows = messy_csv
    synthesize_file(src, src.with_name("synth_clients.csv"), seed=5)
    synth = _read(src.with_name("synth_clients.csv"))
    rare = {r["Status"] for r in rows if r["Status"].startswith("odd-")}
    assert {r["Status"] for r in synth} & rare == set()
    assert {"Active", "Closed"} <= {r["Status"] for r in synth}


def test_seed_makes_output_reproducible(messy_csv, tmp_path):
    src, _ = messy_csv
    synthesize_file(src, tmp_path / "a.csv", seed=9)
    synthesize_file(src, tmp_path / "b.csv", seed=9)
    assert (tmp_path / "a.csv").read_bytes() == (tmp_path / "b.csv").read_bytes()


def test_rows_override_and_dry_run(messy_csv, tmp_path):
    src, _ = messy_csv
    report = synthesize_file(src, tmp_path / "big.csv", rows=1000, seed=1)
    assert len(_read(tmp_path / "big.csv")) == 1000 and report.rows_out == 1000
    dry = synthesize_file(src, tmp_path / "none.csv", dry_run=True)
    assert dry.ok and dry.columns and not (tmp_path / "none.csv").exists()


def test_overrides_force_a_type(messy_csv):
    src, _ = messy_csv
    report = synthesize_file(src, src.with_name("synth_clients.csv"),
                             overrides={"columns": {"Status": "identifier"}})
    assert {c.name: c.semantic for c in report.columns}["Status"] == "identifier"
    with pytest.raises(ValueError, match="Unknown type"):
        synthesize(src, overrides={"columns": {"Status": "nonsense"}})


def test_directory_mode(tmp_path, messy_csv):
    src, _ = messy_csv  # tmp_path/clients.csv
    sub = tmp_path / "sub"
    sub.mkdir()
    pq.write_table(pa.table({
        "sin": [f"{n:09d}" for n in range(100_000_000, 100_000_050)],
        "amount": [float(i) for i in range(50)],
        "paid": [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(50)],
    }), sub / "pay.parquet")
    (tmp_path / "notes.md").write_text("ignored")

    reports = synthesize(tmp_path, recursive=True, seed=1)
    assert sorted(p.name for p in (tmp_path.rglob("synth_*"))) == ["synth_clients.csv", "synth_pay.parquet"]
    assert all(r.ok for r in reports)
    table = pq.read_table(sub / "synth_pay.parquet")
    assert table.schema.field("paid").type == pa.date32() and table.num_rows == 50

    # A rerun skips the synth_ files it made last time.
    assert len(synthesize(tmp_path, recursive=True, dry_run=True)) == 2

    # Non-recursive only sees the top folder; --out-dir mirrors sub-folders.
    out = tmp_path.parent / "mirror"
    synthesize(tmp_path, out, recursive=True, seed=1)
    assert (out / "synth_clients.csv").exists() and (out / "sub" / "synth_pay.parquet").exists()
    assert len(synthesize(tmp_path, dry_run=True)) == 1


def test_strict_fails_when_no_safe_value_exists(tmp_path):
    src = tmp_path / "codes.csv"
    src.write_text("ref_code\n" + "".join(f"{i:03d}\n" for i in range(1000)))
    out = tmp_path / "synth_codes.csv"
    report = synthesize_file(src, out, strict=True)
    assert not report.ok and "LeakError" in report.error
    assert not out.exists()
    relaxed = synthesize_file(src, out)
    assert relaxed.ok and relaxed.columns[0].unresolved > 0


def test_cli_synth(messy_csv, tmp_path):
    import json

    from fakegen.cli import main

    src, _ = messy_csv
    main(["synth", str(src), "--out-dir", str(tmp_path / "cli"), "--seed", "1",
          "--rows", "25", "--report", str(tmp_path / "r.json")])
    assert len(_read(tmp_path / "cli" / "synth_clients.csv")) == 25
    report = json.loads((tmp_path / "r.json").read_text())
    assert report[0]["rows_out"] == 25 and report[0]["columns"][0]["name"] == "SIN"


# ─── --percent and large-file mode ────────────────────────────────────────────

@pytest.mark.parametrize("large_file", [False, True], ids=["normal", "large-file-mode"])
def test_percent_sets_output_size(messy_csv, tmp_path, large_file):
    src, rows = messy_csv
    report = synthesize_file(src, tmp_path / "p.csv", percent=10, seed=1, large_file=large_file)
    assert report.ok, report.error
    assert report.rows_in == len(rows) and report.rows_out == len(rows) // 10
    assert len(_read(tmp_path / "p.csv")) == len(rows) // 10
    # Rounds up, and never produces an empty file from a non-empty source.
    assert synthesize_file(src, tmp_path / "q.csv", percent=0.01).rows_out == 1


def test_percent_and_rows_are_exclusive(messy_csv, tmp_path):
    src, _ = messy_csv
    report = synthesize_file(src, tmp_path / "x.csv", rows=5, percent=5)
    assert not report.ok and "either rows or percent" in report.error
    assert "greater than 0" in synthesize_file(src, tmp_path / "x.csv", percent=0).error


def test_large_file_mode_matches_normal_mode_shape(messy_csv, tmp_path):
    src, rows = messy_csv
    normal = synthesize_file(src, tmp_path / "n.csv", seed=1)
    large = synthesize_file(src, tmp_path / "l.csv", seed=1, large_file=True)
    assert large.ok, large.error
    # Same source, same detections -- the sample is just chosen differently.
    assert [c.semantic for c in large.columns] == [c.semantic for c in normal.columns]
    out = (tmp_path / "l.csv").read_bytes()
    assert out.split(b"\r\n")[0] == src.read_bytes().split(b"\r\n")[0]
    # Seeded large-file runs are reproducible too, and leave no temp files behind.
    synthesize_file(src, tmp_path / "l2.csv", seed=1, large_file=True)
    assert (tmp_path / "l2.csv").read_bytes() == out
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]


def test_large_file_mode_parquet(tmp_path):
    src = tmp_path / "pay.parquet"
    pq.write_table(pa.table({
        "sin": [f"{n:09d}" for n in range(100_000_000, 100_000_400)],
        "amount": [float(i) for i in range(400)],
        "paid": [dt.date(2024, 1, 1) + dt.timedelta(days=i % 300) for i in range(400)],
    }), src)
    report = synthesize_file(src, tmp_path / "synth_pay.parquet", percent=25, large_file=True, seed=1)
    assert report.ok, report.error
    table = pq.read_table(tmp_path / "synth_pay.parquet")
    assert table.num_rows == 100 and table.schema.field("paid").type == pa.date32()
    real = set(pq.read_table(src).column("sin").to_pylist())
    assert not real & set(table.column("sin").to_pylist())


def test_large_file_mode_leak_check_catches_collisions(tmp_path):
    # Every 3-letter code exists in the source, so every generated one leaks.
    src = tmp_path / "codes.csv"
    src.write_text("ref_code\n" + "".join(f"{i:03d}\n" for i in range(1000)))
    report = synthesize_file(src, tmp_path / "o.csv", large_file=True)
    col = report.columns[0]
    assert report.ok and col.collisions == 1000 and col.unresolved > 0
    strict = synthesize_file(src, tmp_path / "s.csv", large_file=True, strict=True)
    assert not strict.ok and "LeakError" in strict.error and not (tmp_path / "s.csv").exists()


def test_cli_percent(messy_csv, tmp_path):
    from fakegen.cli import main

    src, rows = messy_csv
    main(["synth", str(src), "--out-dir", str(tmp_path / "cli"), "--percent", "5", "--large-file-mode", "on"])
    assert len(_read(tmp_path / "cli" / "synth_clients.csv")) == len(rows) // 20
    with pytest.raises(SystemExit):
        main(["synth", str(src), "--rows", "5", "--percent", "5"])


def test_large_file_leak_scan_handles_cp1252_and_bom(tmp_path):
    from fakegen.synth.io import open_source
    from fakegen.synth.scan import DuckSource

    # Curly quotes, dashes and Š/Œ are bytes 0x80-0x9F in Windows-1252.
    rows = "name;sin\n“ŠIMON” Œuvre – Jr;046 454 286\nAnna Smith;130692544\n"
    cands = ["šimon œuvre jr", "Œuvre, Šimon Jr", "046-454-286", "130 692 544", "Anna Smyth", "999 999 999"]
    for encoding in ("cp1252", "utf-8-sig"):
        src = tmp_path / f"people-{encoding}.csv"
        src.write_bytes(rows.encode(encoding))
        work = tmp_path / f"work-{encoding}"
        work.mkdir()
        source = DuckSource(open_source(src), work)
        assert source.count() == 2
        assert source.sample(None, 2, 0)[0][0] in {"“ŠIMON” Œuvre – Jr", "Anna Smith"}
        path = tmp_path / "cand.parquet"
        pq.write_table(pa.table({"id": pa.array(range(len(cands)), pa.int64()), "v": cands}), path)
        assert source.find_leaks(path, [0, 1]) == {0, 1, 2, 3}, encoding
        source.close()
