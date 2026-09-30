"""
fakegen.synth -- synthetic twins of messy, sensitive CSV/TXT/Parquet files.

    from fakegen import synthesize
    reports = synthesize("real_data/")          # every file in the folder
    reports = synthesize("real_data/clients.csv", seed=42)

Each report says what was detected in every column and how the leak guard
did -- it never contains real values. See the README for details.
"""

from .detect import SEMANTIC_TYPES, SENSITIVE_TYPES
from .guard import LeakError
from .runner import (
    ColumnReport, FileReport, find_source_files, load_overrides, synthesize, synthesize_file,
)

__all__ = [
    "synthesize", "synthesize_file", "find_source_files", "load_overrides",
    "FileReport", "ColumnReport", "LeakError", "SEMANTIC_TYPES", "SENSITIVE_TYPES",
]
