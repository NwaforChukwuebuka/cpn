# Module D — Profile Builder

Builds `profile.json` from a template and optional `full_cpn.json` / `verification.json`. No browser automation; config and validation only. Output is used by **Module E** (e.g. Capital One) to fill applications.

## Inputs

- **`profile_template.json`** — Base profile (name, address, phone, email, DOB, income, job, time at address/job).
- **`data/full_cpn.json`** (optional) — Provides `cpn` (SSN) from Module B; if present, `full` is copied into the profile.
- **`data/verification.json`** (optional) — Read for logging only; not written into `profile.json`.

## Output

- **`data/profile.json`** — Single profile with:
  - `cpn`, `ssn_formatted` (for forms)
  - `first_name`, `middle_initial`, `last_name`
  - `capital_one.legal_first_name`, `legal_middle_initial`, `legal_last_name` (for Capital One step 1)
  - `email`, `phone`
  - `address` (with `street`, `city`, `state`, `zip`, `country`, `full` line)
  - `date_of_birth` (MM/DD/YYYY)
  - `annual_income` (50k–80k), `job_type` (Self Employed), `time_at_address`, `time_on_job`

## Validation

- **Annual income:** must be in $50,000–$80,000.
- **Job type:** must be `Self Employed`.
- **Time at address / time on job:** expected `5 Years 5 Months` (warnings only unless `--strict`).

## Run

From project root:

```bash
python -m modules.profile_builder.build
```

Or:

```bash
python modules/profile_builder/build.py
```

Options:

- `--template` — Path to profile template (default: `modules/profile_builder/profile_template.json`).
- `--full-cpn` — Path to `full_cpn.json` (default: `data/full_cpn.json`).
- `--verification` — Path to `verification.json` (default: `data/verification.json`).
- `-o` / `--output` — Output path (default: `data/profile.json`).
- `--strict` — Exit with code 1 on any validation warning.

## Editing the profile

Edit **`profile_template.json`** with your name, address, phone, email, DOB, income, and job/time-at-address/time-on-job. Run the builder again after changing the template or after Module B updates `full_cpn.json`.
