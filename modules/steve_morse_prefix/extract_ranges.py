"""One-time script: extract state->area ranges from stevemorse.html into state_area_ranges.json."""
import json
import re
from pathlib import Path

html = Path(__file__).parent.parent.parent / "stevemorse.html"
out_path = Path(__file__).parent / "state_area_ranges.json"
text = html.read_text(encoding="utf-8", errors="replace")
pat = re.compile(r'<option value="([^"]+)">(\d{3}) to (\d{3})</option>')
out = []
for m in pat.finditer(text):
    out.append({"label": m.group(1), "low": int(m.group(2)), "high": int(m.group(3))})
out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
print(f"Wrote {len(out)} entries to {out_path}")
