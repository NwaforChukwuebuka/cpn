# Module E — Capital One Credit Card Application Filler

Fills the Capital One Platinum (productId=37216) 8-step application using **data/profile.json** and Playwright. **Uses AdsPower only** (no local browser launch). Writes **data/tri_merge_log.json** with steps completed and any errors.

**Concurrent execution:** For multi-user use (e.g. Telegram bot), use `run_filler_from_data()` or `run_filler_async()` with in-memory `profile` and `steps_config`. Pass a distinct `adspower_profile` per session (e.g. from a profile pool). Optional `log_callback` for per-session logging; optional `log_path` to write log JSON, or omit to get `log_entry` in the result.

## Input

- **data/profile.json** — From Module D. Uses:
  - `capital_one.legal_first_name`, `legal_middle_initial`, `legal_last_name`
  - `date_of_birth`, `ssn_formatted`
  - `address.street`, `address.city`, `address.state`, `address.zip`
  - `annual_income`, `job_type`, `time_at_address`, `time_on_job`
  - `phone`, `email`

## Output

- **data/tri_merge_log.json** — One entry per run: `timestamp`, `product`, `url`, `steps_completed`, `ok`, `errors`, `save_html_dir`.
- **data/capital_one_pages/** — Each step’s page HTML is saved by default for later analysis:
  - `step_01_1_of_8.html`, `step_02_2_of_8.html`, … `step_08_8_of_8.html`
  - Use `--no-save-html` to disable, or `--save-html-dir DIR` to use another directory.

## Config

- **modules/capital_one/steps.json** — Step-by-step field mapping. Each step has:
  - `step`, `description`
  - `fields`: list of `{ profile_key, label?, placeholder? }`
  - `continue_button` (default "Continue")
  - `continue_selector` (optional): Playwright selector if the default button detection fails (e.g. `"button:has-text('Continue')"`)

Update `steps.json` when the Capital One form changes or when you discover steps 6–8 (review/terms/submit).

## API (concurrent / bot use)

```python
from modules.capital_one import run_filler_from_data, run_filler_async, load_json

# Load steps config once (or from cache)
steps_config = load_json(Path("modules/capital_one/steps.json"))  # or pass dict

# Sync: pass per-session profile and steps_config; use distinct adspower_profile per user
result = run_filler_from_data(
    profile=session_profile,
    steps_config=steps_config,
    log_path=session_log_path,  # or None to get result["log_entry"]
    adspower_profile=profile_pool.get_for_user(user_id),
    log_callback=lambda msg: send_to_user(user_id, msg),
)

# Async (non-blocking)
result = await run_filler_async(
    profile=session_profile,
    steps_config=steps_config,
    adspower_profile=session_profile_id,
)
# result["ok"], result["steps_completed"], result.get("log_entry")
```

## Run (CLI)

**Requires:** AdsPower running with Local API enabled (default `http://127.0.0.1:50325`). Create an AdsPower browser profile and pass its ID.

From project root:

```bash
# Use default AdsPower profile (k19jxste)
python -m modules.capital_one.run_filler

# Only fill step 1 (name) then stop
python -m modules.capital_one.run_filler --stop-after 1

# Custom AdsPower profile and API
python -m modules.capital_one.run_filler --adspower-profile YOUR_PROFILE_ID --adspower-api http://127.0.0.1:50325
```

Options:

- `--profile` — Path to profile.json (default: data/profile.json).
- `--steps` — Path to steps.json (default: modules/capital_one/steps.json).
- `--log` — Path to tri_merge_log.json (default: data/tri_merge_log.json).
- `--adspower-profile` — AdsPower profile ID (default: k19jxste).
- `--adspower-api` — AdsPower Local API base URL (default: http://127.0.0.1:50325).
- `--stop-after N` — Stop after step N (e.g. 1 to only fill name and click Continue).
- `--save-html-dir DIR` — Directory for step HTML (default: data/capital_one_pages). Each 1/8…8/8 page is saved as step_NN_N_of_8.html.
- `--no-save-html` — Do not save step HTML.
- `--pause-after-fill SECS` — Pause this many seconds after filling each step (before clicking Continue) so you can visually cross-check (default: 5). Use `--pause-after-fill 0` to disable.
- `--project-root` — Project root for default paths.

## Notes

- **AdsPower required:** The filler always uses an AdsPower browser profile (no local Chromium launch). Install AdsPower, enable Local API, and create a profile; use `--adspower-profile` if different from the default.
- **Dependencies:** `playwright` and `requests` (for AdsPower API). Run `pip install playwright requests` and `playwright install chromium`.
- **High risk:** Submitting real applications. Use `--stop-after 1` to test; review each step before submitting.
- Capital One may use different labels/placeholders; adjust **steps.json** to match the live form.
- Steps 6–8 may be review, terms, or final submit — add them to **steps.json** as you discover them.
