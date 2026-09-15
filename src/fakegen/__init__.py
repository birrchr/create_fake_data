"""fakegen — YAML/CSV-driven synthetic data generation, powered by Faker + DuckDB."""

from .generate import generate_dataset
from .schema import ColumnSpec, DatasetSpec, load_schema, validate_schema

__version__ = "0.1.0"
__all__ = [
    "generate_dataset",
    "ColumnSpec",
    "DatasetSpec",
    "load_schema",
    "validate_schema",
]
