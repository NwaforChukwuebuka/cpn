# Module A — Steve Morse SSN Prefix (Steps 1–2)

Gets a valid partial CPN (AAA-GG-XXXX) from the Steve Morse decoding rules.

## Setup (venv)

From the project root (`cpn/`), use a virtual environment:

**Windows (PowerShell):**
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

**Windows (cmd):**
```cmd
python -m venv venv
venv\Scripts\activate.bat
pip install -r requirements.txt
playwright install chromium
```

**macOS / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Then run any of the commands below from the same shell (with venv activated).

## Input

- **state** — e.g. `"Florida"`, `"California"`. For states with multiple area ranges (e.g. Louisiana 1936–1999 vs 2000–....), the module **always** uses the latest “was issued in” period only (e.g. Louisiana (2000-....), never Louisiana (1936-1999)).
- **config** (optional) — `prefer_recent` is ignored; latest issuance date is always used.

## Output

Writes **`data/partial_cpn.json`** (or path given by `--output`):

```json
{
  "area_range": [766, 772],
  "area": "772",
  "group": "11",
  "partial": "772-11-XXXX",
  "state": "Florida",
  "date_range_used": "Florida (2001-....)",
  "error": null,
  "ok": true
}
```

On error, `ok` is `false`, `error` is set, and other fields may be `null`.

## Run

From repo root (`cpn/`):

```bash
# Default state Florida, writes data/partial_cpn.json
python -m modules.steve_morse_prefix

# Explicit state
python -m modules.steve_morse_prefix California

# Custom output and data file
python -m modules.steve_morse_prefix Florida -o data/partial_cpn.json -d modules/steve_morse_prefix/state_area_ranges.json
```

Or run the script directly:

```bash
python modules/steve_morse_prefix/steve_morse.py Florida -o data/partial_cpn.json
```

## Data source

The state→area mapping is read from **`state_area_ranges.json`** in this module (collated from the Steve Morse page; no HTML parsing at runtime). The file is an array of `{ "label": "Florida (2001-....)", "low": 766, "high": 772 }`. To regenerate it from a new HTML snapshot, run:

```bash
python modules/steve_morse_prefix/extract_ranges.py
```

(Requires `stevemorse.html` in the repo root.)

## Playwright automation (five-digit decoder)

To get a **verified** 5-digit prefix (area + group) using the live Steve Morse site (Steps 2b + 4 in `note.md`), use the venv above, then:

```bash
# Run (default state Florida; uses 20s delay between group tries to reduce rate limiting)
python -m modules.steve_morse_prefix.run_five_digit_decoder Florida --headless

# With output file and shorter delay (faster but higher rate-limit risk)
python -m modules.steve_morse_prefix.run_five_digit_decoder California -o data/partial_cpn.json --delay 5 --headless
```

Output is JSON, e.g. `{ "ok": true, "prefix_5": "769-99", "area": "769", "group": "99", "state": "Florida", "date_range_used": "Florida (2001-....)", "verified_range": [766, 772] }`. See `INVESTIGATION_STEPHEN_MORSE_FLOW.md` for selectors and behavior.

## Failure isolation

If the data file is missing or the state is unknown, this module writes the error into the output JSON and returns a non-zero exit code. Downstream modules (B, C, …) should not run until `partial_cpn.json` has `ok === true`.

## Concurrent usage (async / many parallel users)

The module is designed for **concurrent use**: multiple users (e.g. in a Telegram bot) can call it at the same time without conflicts or blocking each other.

- **Stateless logic** — `steve_morse.py` has no shared mutable state; all functions are pure given their inputs.
- **Async entrypoints** — Use these from an async event loop (e.g. aiogram) so the loop is not blocked:
  - `async_get_partial_cpn(state, data_path=None, *, prefer_recent=True)` → same result as `get_partial_cpn`, runs in a thread pool.
  - `async_get_latest_state_range(state, data_path=None)` → same as `get_latest_state_range`, runs in a thread pool.
  - `async_run(state, output_path, config_path=None, data_path=None)` → same as `run`, runs in a thread pool.
  - `async_run_five_digit_decoder(state, *, data_path=None, delay_seconds=20, headless=True)` → runs the Playwright flow in a thread pool; each concurrent call uses its own browser instance, so many users can run in parallel.

Example from an async bot:

```python
from modules.steve_morse_prefix import async_get_partial_cpn, async_run_five_digit_decoder

# Non-blocking; safe to await from many concurrent user sessions
result = await async_get_partial_cpn("Florida")
verified = await async_run_five_digit_decoder("California", delay_seconds=15, headless=True)
```

The architecture is **scalable and asynchronous**: I/O and CPU work run off the event loop via `asyncio.to_thread`, so one process can handle many parallel user sessions without blocking.
