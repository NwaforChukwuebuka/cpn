# Module B — SSN Validator / Deceased Check (Step 3)

Completes the 9-digit CPN (adds last 4 digits) and checks the SSA Death Masterfile and issuance status at [SSN-Verify.com](https://www.ssn-verify.com/). Uses an **AdsPower** browser profile so automation runs in that profile’s fingerprint/session.

**Concurrent execution:** For multi-user use (e.g. Telegram bot), use `run_validation()` or `run_validation_async()` with in-memory `partial_cpn` and a **distinct AdsPower profile per session** (e.g. from a profile pool). No global state; each run is isolated.

## Input

- **`partial_cpn`** (dict or file), e.g.:
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

## API (concurrent / bot use)

```python
from modules.ssn_validator import run_validation, run_validation_async, normalize_partial

# Sync: pass per-user partial_cpn and profile (different profile per session)
result = run_validation(
    partial_cpn={"area": "772", "group": "11"},
    last_four="5245",
    adspower_profile=session_profile_id,
    adspower_api_base="http://127.0.0.1:50325",
)

# Async (non-blocking): same contract
result = await run_validation_async(
    partial_cpn={"prefix_5": "769-99"},
    adspower_profile=session_profile_id,
)
```

Each concurrent user must use a **different** `adspower_profile`. No shared files or global state.

## Prerequisites

1. **AdsPower** installed and running (Local API enabled).
2. **Browser profile(s)** in AdsPower; for concurrent use, one profile per simultaneous session.
3. **Python venv** with dependencies (see repo root):
   ```bash
   pip install playwright requests httpx
   playwright install chromium   # optional if using only AdsPower browser
   ```

## Run (CLI)

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
- For multiple users: assign each session a different profile (e.g. from a pool).

## Failure isolation

If SSN-Verify is down or the page layout changes, the module returns a result dict with `ok: false` and `error` set. When using the API, no shared files are written; each call gets its own result.
