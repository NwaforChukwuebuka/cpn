# CPN Automation Plan — Separation of Concerns

Each automation step is a **separate module** with its own inputs, outputs, and error handling. One failing site or change in UI does not break the rest of the pipeline. Modules communicate via **files or a simple data contract** (e.g. JSON), not tight coupling.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         ORCHESTRATOR / RUNNER (optional)                      │
│  Runs modules in order, passes output of N as input to N+1, handles retries   │
└─────────────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│ MODULE A         │───▶│ MODULE B         │───▶│ MODULE C         │
│ Steve Morse      │    │ SSN Validator    │    │ SearchBug        │
│ (prefix lookup)  │    │ (deceased check) │    │ (InstantID scan) │
└──────────────────┘    └──────────────────┘    └──────────────────┘
        │                         │                        │
        ▼                         ▼                        ▼
   partial_cpn.json           full_cpn.json            verification.json
   (772-11-XXXX)              (772-11-5245)             (status: inactive)
        │                         │                        │
        └─────────────────────────┴────────────────────────┘
                                    │
                                    ▼
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│ MODULE D         │    │ MODULE E         │    │ MODULE F         │
│ Profile Builder  │    │ Tri-Merge        │    │ Public Records   │
│ (config/template)│    │ (optional)       │    │ ListYourself     │
└──────────────────┘    └──────────────────┘    └──────────────────┘
```

---

## Module A — Steve Morse SSN Prefix (Steps 1–2)

**Responsibility:** Get a valid partial CPN (AAA-GG-XXXX) from stevemorse.org only.

**Input:**  
- `state` (e.g. "Florida")  
- Optional: `config.json` (rate limit delay, user-agent, etc.)

**Output:**  
- `partial_cpn.json`  
  - `area_range`: e.g. `[766, 772]`  
  - `area`: e.g. `772`  
  - `group`: e.g. `11`  
  - `partial`: e.g. `"772-11-XXXX"`  
  - `state`, `date_range_used`

**Failure isolation:**  
- If Steve Morse is down, changed, or blocks IP: this module fails and returns an error. No other module runs until a valid `partial_cpn.json` exists (or you run Module A again later).  
- Implement rate limiting (e.g. 1 request per 20+ seconds) and random selection within the area range to avoid blocks.

**Suggested file:** `modules/steve_morse_prefix/` or `automation/steve_morse.py`

---

## Module B — SSN Validator / Deceased Check (Step 3)

**Responsibility:** Complete the last 4 digits and check deceased file at https://www.ssn-verify.com/

**Input:**  
- `partial_cpn.json` (from Module A)  
- Optional: `last_four` (if you want to fix last 4) or let module choose randomly

**Output:**  
- `full_cpn.json`  
  - `full`: e.g. `"772-11-5245"`  
  - `last_four`: e.g. `"5245"`  
  - `deceased_check`: "no_record" | "error" | "found"  
  - `raw_result` or `summary` (for debugging)

**Failure isolation:**  
- If SSNVerify is down or layout changes: Module B fails and writes an error. Module C does not run. You can re-run B when the site is back or after fixing selectors.  
- No dependency on Module A’s internal logic—only on the existence and format of `partial_cpn.json`.

**Implementation:** `modules/ssn_validator/` — `python -m modules.ssn_validator` (reads `data/partial_cpn.json`, writes `data/full_cpn.json`). Site may block automation (Cloudflare); see `modules/ssn_validator/README.md` and `selectors.json`.

---

## Module C — SearchBug / InstantID Verification (Step 4)

**Responsibility:** Verify the full CPN shows “Reserved for future use” or “Inactive” at one verification service (e.g. SearchBug).

**Input:**  
- `full_cpn.json` (from Module B)

**Output:**  
- `verification.json`  
  - `cpn`: full number  
  - `status`: "inactive" | "reserved_for_future_use" | "issued" | "error"  
  - `state_of_issuance`, `year_of_issuance` (if present)  
  - `ok`: boolean (true only if status is inactive or reserved_for_future_use)

**Failure isolation:**  
- If SearchBug changes UI or requires login: only Module C fails. You can switch to another verification URL or service by changing only Module C.  
- Orchestrator can retry with a new CPN (back to Module A/B) if `ok === false`.

**Suggested file:** `modules/searchbug_verify/` or `automation/searchbug_verify.py`

---

## Module D — Profile Builder (Steps 5–8)

**Responsibility:** Hold profile rules and optionally generate/template the data. **No browser automation required**; this is config and validation.

**Input:**  
- Optional: `profile_template.json` or env (address, phone, email, income range, job, time at address/job)  
- Optional: `verification.json` (to attach CPN to profile)

**Output:**  
- `profile.json`  
  - `cpn`  
  - `address`, `phone`, `email`  
  - `annual_income`, `time_at_address`, `job_type`, `time_on_job`  
  - Validation: income in $50k–$80k, job = Self Employed, times = 5y 5mo, etc.

**Failure isolation:**  
- Purely data. If you change rules (e.g. income range), only this module or its config changes. No impact on Steve Morse, SSNVerify, or SearchBug.

**Suggested file:** `modules/profile_builder/` or `config/profile_schema.json` + `automation/profile_builder.py`

---

## Module E — Tri-Merge (Step 9) — Optional / Manual

**Responsibility:** Document which applications to submit and when. Automation here is high-risk (submitting real applications) and site-specific; better as **manual** or **semi-automated** (e.g. open pre-filled forms).

**Input:**  
- `profile.json`  
- Optional: list of product URLs (e.g. Capital One, First Premier)

**Output:**  
- `tri_merge_log.json` (which applications were “submitted”/opened, timestamps)  
- Or just instructions for the user

**Failure isolation:**  
- Kept separate so that any tri-merge or bank-site change does not affect number generation or verification. Can be disabled in the orchestrator.

**Suggested file:** `modules/tri_merge/` (or `docs/tri_merge_instructions.md` only)

---

## Module F — Public Records / ListYourself (Step 10)

**Responsibility:** Fill and submit the form at listyourself.net using data from `profile.json`.

**Input:**  
- `profile.json` (after 48h delay from tri-merge)

**Output:**  
- `public_records_submitted.json` (timestamp, success/failure, optional receipt or screenshot path)

**Failure isolation:**  
- If ListYourself changes: only Module F breaks. Number generation and verification are unchanged.  
- Orchestrator can enforce “wait 48 hours after Step 9” before calling this module.

**Suggested file:** `modules/listyourself/` or `automation/listyourself.py`

---

## Module G — CreditKarma Verification (Step 11) — Optional

**Responsibility:** Check that the CPN profile can sign up and see expected inquiry (optional automation; often better as manual).

**Input:**  
- `profile.json`  
- Optional: credentials or session (if automated)

**Output:**  
- `creditkarma_result.json` (success, errors, or “manual_check_required”)

**Failure isolation:**  
- Separate from all other steps. If CreditKarma changes flow or blocks automation, only this module is updated or disabled.

**Suggested file:** `modules/creditkarma_verify/` or `automation/creditkarma_verify.py`

---

## Data Contract (Shared)

Use a **shared directory** (e.g. `./data/` or `./output/`) and agreed file names so modules do not depend on each other’s code:

```text
data/
  partial_cpn.json    # from Module A
  full_cpn.json      # from Module B
  verification.json  # from Module C
  profile.json       # from Module D
  tri_merge_log.json
  public_records_submitted.json
  creditkarma_result.json
```

Or a single `pipeline_state.json` that the orchestrator updates after each step.

---

## Orchestrator (Optional)

A small **runner** script or config that:

1. Runs Module A → if success, run B; else stop and report.  
2. Runs Module B → if success, run C; else stop and report.  
3. Runs Module C → if `verification.json.ok` then continue to D; else optionally retry from A with new number.  
4. Runs Module D (profile).  
5. Optionally runs E, F, G when conditions (e.g. 48h delay) are met.

Each step should be **runnable standalone** for debugging (e.g. “run only Module C with this `full_cpn.json`”). That keeps separation of concerns: one problem stays in one module.

---

## Summary Table

| Module | Step(s) | Site / Source        | Fails independently? | Output file(s)        |
|--------|---------|----------------------|------------------------|------------------------|
| A      | 1–2     | stevemorse.org       | Yes                   | partial_cpn.json      |
| B      | 3       | ssn-verify.com/     | Yes                   | full_cpn.json         |
| C      | 4       | searchbug.com (or other) | Yes               | verification.json     |
| D      | 5–8     | Config / template    | Yes                   | profile.json          |
| E      | 9       | Banks    | Yes                   | tri_merge_log.json    |
| F      | 10      | listyourself.net     | Yes                   | public_records_*.json |
| G      | 11      | creditkarma.com      | Yes                   | creditkarma_result.json |

This plan gives you **separation of concerns**: one module per automation concern, one failure domain per module, and a clear data flow between them.
