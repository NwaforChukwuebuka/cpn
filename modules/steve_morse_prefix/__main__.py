"""
Entry point for: python -m modules.steve_morse_prefix [STATE] [options]

Avoids the RuntimeWarning that occurs when running the submodule directly
(python -m modules.steve_morse_prefix.steve_morse), which loads the same
module twice (once via __init__.py import, once as __main__).
"""

import argparse
import json
from pathlib import Path

from .steve_morse import run


def main() -> None:
    parser = argparse.ArgumentParser(description="Module A: Steve Morse SSN prefix → partial_cpn.json")
    parser.add_argument("state", nargs="?", default="Florida", help="State name (e.g. Florida)")
    parser.add_argument("--output", "-o", type=Path, default=Path("data/partial_cpn.json"), help="Output JSON path")
    parser.add_argument("--config", "-c", type=Path, default=None, help="Optional config.json")
    parser.add_argument("--data", "-d", type=Path, default=None, help="Path to state_area_ranges.json")
    args = parser.parse_args()
    ok = run(args.state, args.output, args.config, args.data)
    print(json.dumps(json.loads(args.output.read_text()), indent=2))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
