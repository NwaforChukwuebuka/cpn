"""
Module A — Steve Morse SSN Prefix (Steps 1–2)

Gets a valid partial CPN (AAA-GG-XXXX) using the Steve Morse decoding rules.
Uses collated state→area data from state_area_ranges.json (no HTML parsing).
Always picks the area range with the latest "was issued in" date for the state
(e.g. Louisiana (2000-....) not Louisiana (1936-1999)).
"""

import json
import random
import re
from pathlib import Path
from typing import Optional

_here = Path(__file__).resolve().parent
DEFAULT_DATA_PATH = _here / "state_area_ranges.json"

# Labels to skip when resolving by state (invalid or special use)
SKIP_LABELS = (
    "not valid",
    "unassigned",
    "Individual Taxpayer Identification Number",
    "Railroad workers",
    "Enumeraton at Entry",
    "North Carolina and West Virginia",  # ambiguous single number
)


def _latest_issuance_year(label: str) -> int:
    """
    Extract the end year of the issuance period from a label for comparison.
    "(YYYY-....)" = ongoing → 9999 (always latest).
    "(YYYY-YYYY)" = ended range → second YYYY.
    No parentheses (e.g. "Ohio") → 9999 (single period).
    """
    if "...." in label:
        return 9999
    m = re.search(r"\((\d{4})-(\d{4})\)", label)
    if m:
        return int(m.group(2))
    return 9999


def _ranges_with_latest_date_only(
    ranges: list[tuple[int, int, str]],
) -> list[tuple[int, int, str]]:
    """Keep only ranges that have the latest issuance date for this state."""
    if len(ranges) <= 1:
        return ranges
    max_year = max(_latest_issuance_year(r[2]) for r in ranges)
    return [r for r in ranges if _latest_issuance_year(r[2]) == max_year]


def load_ranges(data_path: Optional[Path] = None) -> list[dict]:
    """Load state/area ranges from JSON. Each entry: { \"label\": str, \"low\": int, \"high\": int }."""
    path = data_path or DEFAULT_DATA_PATH
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def build_state_to_ranges(entries: list[dict]) -> dict[str, list[tuple[int, int, str]]]:
    """Map state name (normalized) to list of (low, high, label). Queryable by state name (e.g. 'Florida')."""
    by_state: dict[str, list[tuple[int, int, str]]] = {}
    for item in entries:
        label = item.get("label", "")
        low = int(item.get("low", 0))
        high = int(item.get("high", 0))
        if any(skip in label for skip in SKIP_LABELS):
            continue
        if low < 1 or high > 799:
            continue
        normalized = label.split("(")[0].strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key not in by_state:
            by_state[key] = []
        by_state[key].append((low, high, label))
    return by_state


def get_latest_state_range(
    state: str,
    data_path: Optional[Path] = None,
) -> Optional[dict]:
    """
    Return the latest issuance period for a state from config: label, low, high.
    Use for live verification (Step 2) and to get the state option label for the site.

    Returns None if state/data not found; otherwise
    { "label": "Florida (2001-....)", "low": 766, "high": 772 }.
    """
    path = data_path or DEFAULT_DATA_PATH
    if not path.is_file():
        return None
    entries = load_ranges(path)
    by_state = build_state_to_ranges(entries)
    state_key = state.strip().lower()
    if state_key not in by_state:
        candidates = [k for k in by_state if state_key in k or k in state_key]
        if not candidates:
            return None
        state_key = candidates[0]
    ranges = _ranges_with_latest_date_only(by_state[state_key])
    if not ranges:
        return None
    low, high, label = ranges[0]
    return {"label": label, "low": low, "high": high}


def get_partial_cpn(
    state: str,
    data_path: Optional[Path] = None,
    *,
    prefer_recent: bool = True,
) -> dict:
    """
    Resolve state to an area range, pick random area and group, return partial CPN data.

    Always uses the latest "was issued in" period for the state (e.g. Louisiana (2000-....)
    not Louisiana (1936-1999)). prefer_recent is kept for backward compatibility but has no
    effect; latest date is always used.

    state: e.g. "Florida", "California"
    data_path: path to state_area_ranges.json; if None, uses DEFAULT_DATA_PATH.
    prefer_recent: ignored; latest issuance date is always used.

    Returns dict with: area_range, area, group, partial, state, date_range_used, error (if any).
    """
    path = data_path or DEFAULT_DATA_PATH
    if not path.is_file():
        return {
            "error": f"State/area data not found: {path}",
            "partial": None,
            "area": None,
            "group": None,
            "area_range": None,
            "state": state,
            "date_range_used": None,
        }

    entries = load_ranges(path)
    by_state = build_state_to_ranges(entries)

    state_key = state.strip().lower()
    if state_key not in by_state:
        candidates = [k for k in by_state if state_key in k or k in state_key]
        if not candidates:
            return {
                "error": f"Unknown state: {state}. Known: {sorted(by_state.keys())}",
                "partial": None,
                "area": None,
                "group": None,
                "area_range": None,
                "state": state,
                "date_range_used": None,
            }
        state_key = candidates[0]

    ranges = by_state[state_key]
    # Always pick from the latest issuance period only (e.g. Louisiana (2000-....) not (1936-1999))
    ranges = _ranges_with_latest_date_only(ranges)
    low, high, date_label = random.choice(ranges)
    area = random.randint(low, high)
    group = random.randint(1, 99)
    area_str = f"{area:03d}"
    group_str = f"{group:02d}"
    partial = f"{area_str}-{group_str}-XXXX"

    return {
        "area_range": [low, high],
        "area": area_str,
        "group": group_str,
        "partial": partial,
        "state": state,
        "date_range_used": date_label,
        "error": None,
    }


def run(
    state: str,
    output_path: Path,
    config_path: Optional[Path] = None,
    data_path: Optional[Path] = None,
) -> bool:
    """
    Run Module A: compute partial CPN for state and write partial_cpn.json.

    Returns True on success, False on error (and writes error info to output).
    """
    config = {}
    if config_path and config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as e:
            config = {"config_load_error": str(e)}

    prefer_recent = config.get("prefer_recent", True)
    result = get_partial_cpn(state, data_path, prefer_recent=prefer_recent)

    if result.get("error"):
        result["ok"] = False
    else:
        result["ok"] = True

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result.get("ok", False) is True


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Module A: Steve Morse SSN prefix → partial_cpn.json")
    parser.add_argument("state", nargs="?", default="Florida", help="State name (e.g. Florida)")
    parser.add_argument("--output", "-o", type=Path, default=Path("data/partial_cpn.json"), help="Output JSON path")
    parser.add_argument("--config", "-c", type=Path, default=None, help="Optional config.json")
    parser.add_argument("--data", "-d", type=Path, default=None, help="Path to state_area_ranges.json")
    args = parser.parse_args()
    ok = run(args.state, args.output, args.config, args.data)
    print(json.dumps(json.loads(args.output.read_text()), indent=2))
    raise SystemExit(0 if ok else 1)
