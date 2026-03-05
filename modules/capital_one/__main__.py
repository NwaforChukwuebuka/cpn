"""Run the Capital One filler when using: python -m modules.capital_one"""
from __future__ import annotations

import sys

from .run_filler import main

if __name__ == "__main__":
    sys.exit(main())
