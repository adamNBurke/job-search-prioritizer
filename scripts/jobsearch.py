# /// script
# requires-python = ">=3.11,<3.16"
# dependencies = ["requests==2.34.2", "pypdf==6.21.4", "python-docx==1.2.0"]
# [tool.uv]
# required-version = ">=0.10"
# ///
"""Entry point shim. All logic lives in src/jobsearch."""

import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
# Index 0 is required: this script's own directory holds a module also named jobsearch.
sys.path.insert(0, str(root / "src"))

from jobsearch.cli import main  # noqa: E402

raise SystemExit(main(sys.argv[1:]))
