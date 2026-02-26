# Module B — SSN Validator / Deceased Check (Step 3)

Completes the 9-digit CPN (adds last 4 digits) and checks the SSA Death Masterfile and issuance status at [SSN-Verify.com](https://www.ssn-verify.com/). Uses an **AdsPower** browser profile so automation runs in that profile’s fingerprint/session.

## Input

- **`partial_cpn.json`** (from Module A), e.g.:
  - `prefix_5`: `"769-99"` and `area` / `group`, or
  - `partial`: `"772-11-XXXX"` and `area` / `group`
- Optional: **`last_four`** (4 digits). If omitted, a random value is used.

## Output

- **`full_cpn.json`**:
  - `full`: e.g. `"772-11-5245"`
  - `last_four`: e.g. `"5245"`
  - `deceased_check`: `"no_record"` | `"found"` | `"error"`
  - `issuance_status`: e.g. `"not_issued"` | `"issued"` | `"error"`
  - `ok`: `true` only when Death Masterfile is “No record” and status indicates not issued
  - `raw_result` / `summary` for debugging

## Prerequisites

1. **AdsPower** installed and running (Local API enabled).
2. **Browser profile** created in AdsPower; default profile ID used by this module: **`k19jxstf`** (override with `--adspower-profile`).
3. **Python venv** with dependencies (see repo root):
   ```bash
   pip install playwright requests
   playwright install chromium   # optional if using only AdsPower browser
   ```

## Run

From repo root, with venv activated:

```bash
# Use default AdsPower profile k19jxstf, read data/partial_cpn.json, write data/full_cpn.json
python -m modules.ssn_validator

# Custom paths and profile
python -m modules.ssn_validator --input data/partial_cpn.json --output data/full_cpn.json --adspower-profile k19jxstf

# Fix last 4 digits (e.g. 5245)
python -m modules.ssn_validator --last-four 5245
```

## AdsPower

- The script starts the given **AdsPower profile** via Local API (`POST /api/v2/browser-profile/start`), then connects **Playwright** to the browser using the returned Puppeteer WebSocket URL (`connect_over_cdp`).
- Default API base: `http://127.0.0.1:50325`. Override with `--adspower-api`.
- Ensure the profile is **closed** before running so the script can start it cleanly.

## Failure isolation

If SSN-Verify is down or the page layout changes, Module B fails and writes an error into the output. Module C does not run until `full_cpn.json` has `ok === true` (or you re-run B after fixing).
