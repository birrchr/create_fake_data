"""Bundled example content for `fakegen init`, kept in-package so it ships
reliably with the installed package rather than depending on a loose file
path on disk."""

EXAMPLE_SCHEMA_YAML = """\
dataset:
  rows: 1000
  seed: 42
  formats: [csv]
  output_dir: data
  file_stem: synthetic

columns:
  - name: subject_id
    type: sequence
    start: 1
    step: 1

  - name: full_name
    type: faker
    provider: name

  - name: email
    type: faker
    provider: email

  - name: age
    type: faker
    provider: random_int
    kwargs: {min: 18, max: 90}

  - name: treatment_arm
    type: code_set
    values: [Placebo, DrugA, DrugB]
    weights: [0.34, 0.33, 0.33]

  - name: enrollment_date
    type: date_range
    start: "2023-01-01"
    end: "2024-12-31"

  - name: status
    type: constant
    value: Active

  - name: notes
    type: faker
    provider: sentence
    missing_pct: 15
"""

EXAMPLE_COLUMNS_CSV = """\
name,type,provider,kwargs_json,values,weights,min,max,start,end,value,missing_pct
subject_id,sequence,,,,,,,,,,
full_name,faker,name,,,,,,,,,
email,faker,email,,,,,,,,,
age,faker,random_int,"{""min"": 18, ""max"": 90}",,,,,,,,
treatment_arm,code_set,,,Placebo|DrugA|DrugB,0.34|0.33|0.33,,,,,,
enrollment_date,date_range,,,,,,,2023-01-01,2024-12-31,,
status,constant,,,,,,,,,Active,
notes,faker,sentence,,,,,,,,,15
"""
