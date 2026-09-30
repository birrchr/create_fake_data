"""
Build the messy example files in examples/messy/ that the README tutorial uses.

Everything in these files is invented by Faker -- no real person's data --
but deliberately made as messy as a real work extract: mixed formats in one
column, several kinds of blanks, typos, junk values, odd encodings/delimiters.

    uv run python examples/make_messy_examples.py
"""

from __future__ import annotations

import csv
import datetime as dt
import random
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from faker import Faker

OUT = Path(__file__).parent / "messy"
ROWS = 300

rng = random.Random(2024)
fake = Faker(["en_CA", "fr_CA"])
fake.seed_instance(2024)

PROVINCE_FORMS = {"ON": ["ON", "Ontario", "ont.", "ONTARIO"], "QC": ["QC", "Quebec", "Québec", "PQ"],
                  "BC": ["BC", "British Columbia", "B.C."], "AB": ["AB", "Alberta"], "NS": ["NS", "Nova Scotia"]}
POSTAL_FIRST = {"ON": "KLMNP", "QC": "GHJ", "BC": "V", "AB": "T", "NS": "B"}


def messy_sin() -> str:
    digits = "".join(c for c in fake.unique.ssn() if c.isdigit())
    r = rng.random()
    if r < 0.35:
        return f"{digits[:3]} {digits[3:6]} {digits[6:]}"
    if r < 0.60:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    if r < 0.80:
        return digits
    if r < 0.85:
        return f" {digits} "
    return rng.choice(["", "", "N/A", "unknown", digits[:8], "see file", "999-999-99"])


def messy_date(d: dt.date) -> str:
    return rng.choice([d.isoformat(), d.isoformat(), d.strftime("%d/%m/%Y"), d.strftime("%d%b%Y").upper()])


def messy_case(s: str) -> str:
    r = rng.random()
    if r < 0.15:
        return s.upper()
    if r < 0.2:
        return s.lower()
    if r < 0.25:
        return s + "  "
    return s


def clients() -> list[dict]:
    out = []
    for i in range(ROWS):
        first, last = fake.first_name(), fake.last_name()
        prov = rng.choice(list(PROVINCE_FORMS))
        postal = (rng.choice(POSTAL_FIRST[prov]) + fake.postcode()[1:]).upper()
        dob = fake.date_of_birth(minimum_age=18, maximum_age=90)
        service = fake.date_between(start_date=max(dob, dt.date(2015, 1, 1)), end_date=dt.date(2024, 12, 31))
        out.append({
            "client_id": f"CL-{i + 1:06d}",
            "SIN": messy_sin(),
            "First Name": messy_case(first),
            "LAST_NAME": last.upper() if rng.random() < 0.7 else last,
            "Full Name": rng.choice([f"{first} {last}", f"{last}, {first}", f"{first} {fake.first_name()[0]}. {last}"]),
            "Email": rng.choice([fake.email(), fake.email().upper(), "", "none", "no email on file"]),
            "Phone": rng.choice([fake.numerify("(###) ###-####"), fake.numerify("###-###-####"),
                                 fake.numerify("##########"), "", "call office"]),
            "Street Address": messy_case(fake.street_address()) if rng.random() > 0.05 else "",
            "City": messy_case(fake.city()),
            "Prov": rng.choice(PROVINCE_FORMS[prov]),
            "Postal Code": rng.choice([postal[:3] + " " + postal[3:], postal, postal.lower(), " " + postal]),
            "DOB": messy_date(dob) if rng.random() > 0.04 else rng.choice(["", "00/00/0000"]),
            "Service Date": messy_date(service),
            "Status": rng.choice(["Active"] * 6 + ["Inactive"] * 3 + ["Pending", "ACTIVE", "", "Actve"]),
            "Balance": rng.choice([f"${rng.uniform(0, 5000):,.2f}", f"{rng.uniform(-50, 900):.2f}", "0", "", "n/a"]),
            "Notes": rng.choice(["", "", fake.sentence(nb_words=rng.randint(6, 18)),
                                 f"Called {first} on {service:%b %d}; left msg.", "see file"]),
        })
    return out


def visits(client_rows: list[dict]) -> list[dict]:
    out = []
    for i in range(ROWS * 2):
        c = rng.choice(client_rows)
        out.append({
            "visit_id": f"V{rng.randint(10_000, 99_999)}",
            "sin_no": c["SIN"],
            "visit_dt": fake.date_time_between(dt.datetime(2020, 1, 1), dt.datetime(2024, 12, 31)).strftime("%Y-%m-%d %H:%M"),
            "program_code": rng.choice(["P01"] * 20 + ["P02"] * 12 + ["P07"] * 5 + ["X99", "P13"]),
            "worker": fake.name(),
        })
    return out


def payments(client_rows: list[dict]) -> pa.Table:
    n = ROWS
    picks = [rng.choice(client_rows) for _ in range(n)]
    return pa.table({
        "client_sin": pa.array([c["SIN"] or None for c in picks], pa.string()),
        "amount": pa.array([round(rng.uniform(10, 2500), 2) if rng.random() > 0.05 else None for _ in range(n)], pa.float64()),
        "paid_on": pa.array([fake.date_between(dt.date(2021, 1, 1), dt.date(2024, 12, 31)) for _ in range(n)], pa.date32()),
        "cheque_no": pa.array([rng.randint(100_000, 999_999) for _ in range(n)], pa.int64()),
        "method": pa.array([rng.choice(["EFT", "EFT", "EFT", "Cheque", "Cash"]) for _ in range(n)], pa.string()),
    })


def main() -> None:
    (OUT / "archive").mkdir(parents=True, exist_ok=True)
    client_rows = clients()

    # Semicolon-delimited, Windows-1252 encoded, Windows line endings.
    with open(OUT / "sample_clients.csv", "w", encoding="cp1252", errors="replace", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(client_rows[0]), delimiter=";", lineterminator="\r\n")
        w.writeheader()
        w.writerows(client_rows)

    # Tab-delimited .txt with every field quoted.
    visit_rows = visits(client_rows)
    with open(OUT / "visits.txt", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(visit_rows[0]), delimiter="\t", quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(visit_rows)

    pq.write_table(payments(client_rows), OUT / "archive" / "payments.parquet")
    print(f"Wrote example files to {OUT}")


if __name__ == "__main__":
    main()
