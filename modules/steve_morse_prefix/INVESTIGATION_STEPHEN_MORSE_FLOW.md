# Steve Morse SSN Decoder — Playwright Investigation Summary

**Page:** https://stevemorse.org/ssn/ssn.html  
**Date:** 2025-02-24  
**Purpose:** Map the “first five digits” flow to live DOM and behavior for automation.

---

## 1. Page structure (confirmed)

### Three-Digit Decoder
| Purpose | Selector | Notes |
|--------|----------|--------|
| “SSN starting with” (range) | `select[name="ssn"]` | Options like `001 to 003`, `766 to 772`, etc. Updates when “was issued in” changes. |
| “was issued in” (state/period) | `select[name="state"]` | Options like `Florida (2001-....)`, `New Hampshire`, etc. **Use this to choose state + latest period.** |

- Selecting **“Florida (2001-....)”** in `select[name="state"]` automatically sets **“766 to 772”** in `select[name="ssn"]`. So for Step 2b we only need to select the state option; the 3-digit range is then visible for verification.

### Five-Digit Decoder
| Purpose | Selector | Notes |
|--------|----------|--------|
| Area (3 digits) | `select[name="ssn1"]` | Values `001`–`999` (string, zero-padded). |
| Group (2 digits) | `select[name="ssn2"]` | Values `01`–`99`. First option is empty (no value). |
| Result text | `#wherewhen` | `<span id="wherewhen">` — **only reliable output for “was issued in” / “Not Issued”.** |

- Changing `ssn1` or `ssn2` triggers `GetCode()` and updates `#wherewhen` (and triggers a server request; see below).

---

## 2. Result text in `#wherewhen`

- **Issued prefix:** e.g. `769-42: Florida, 2005`  
- **Unissued prefix (valid for our use):** e.g. `769-99: Not Issued`

Automation should treat a prefix as **valid (unused)** when the text of `#wherewhen` **contains** `"Not Issued"` (exact casing as on the page).

---

## 3. Server requests (rate limiting)

The Five-Digit Decoder is **not** pure client-side. Each change of area or group causes a script load from:

- **URL pattern:** `https://stevemorse.org/ssn/ssn.php?ssn1=<area>&ssn2=<group>&code=<...>&noCacheIE=<...>`
- `code` appears to come from `https://stevemorse.org/jcal/proxycode.php`.

So every group try hits the server. The note’s warning applies: use **delays between Five-Digit lookups** (e.g. 20+ seconds if rate limited) and consider IP rotation/VPN if we see “unexplained error” or “Please notify me”.

---

## 4. Automation flow (step-by-step)

1. **Navigate** to `https://stevemorse.org/ssn/ssn.html`.
2. **State / 3-digit verification (Step 2):**
   - Select the **latest** state/period in the Three-Digit Decoder, e.g.  
     `select[name="state"]` → option with label `"Florida (2001-....)"`.
   - Read the current option of `select[name="ssn"]` and parse the range (e.g. `766 to 772` → `[766, 772]`). Optionally compare with `state_area_ranges.json`.
3. **Random area (Step 3):**
   - Pick a random 3-digit area in the verified range (e.g. `769`). Set  
     `select[name="ssn1"]` to the zero-padded value (e.g. `"769"`).
4. **Find unused group (Step 4):**
   - Loop (with safety limit, e.g. 99 tries):
     - Pick a random group `01`–`99`.
     - Set `select[name="ssn2"]` to that value (e.g. `"42"`).  
       (If the first option is empty, use a value that selects a real option.)
     - Wait for the result to load (e.g. wait for `#wherewhen` to update and/or for the `ssn.php` request to finish).
     - Read `document.getElementById('wherewhen').textContent`.
     - If it contains **`"Not Issued"`** → success; first five digits = area + group (e.g. `769-42` only if 42 was “Not Issued”; in our test 769-99 was “Not Issued”).
     - Otherwise (e.g. “Florida, 2005”) → try another group.
   - Between iterations, add a delay (e.g. 20+ seconds) to reduce rate-limit risk.

---

## 5. Selectors reference (for Playwright)

```text
# Three-Digit Decoder — choose state (latest period for that state)
page.locator('select[name="state"]').selectOption({ label: 'Florida (2001-....)' })
# Then read 3-digit range from:
page.locator('select[name="ssn"]').inputValue()  # or get selected option text, e.g. "766 to 772"

# Five-Digit Decoder — set area and group
page.locator('select[name="ssn1"]').selectOption('769')
page.locator('select[name="ssn2"]').selectOption('42')

# Result — read and decide
result = await page.locator('#wherewhen').textContent()
# success = result.includes('Not Issued')
```

---

## 6. State option labels (latest period examples)

For “latest” issuance we need the option whose label ends with “....” or is the most recent period for that state. Examples from the page:

- Florida: `Florida (2001-....)`
- California: `California (1987-....)`
- Texas: `Texas (1988-....)`
- etc.

Automation can either:
- Map state code (e.g. FL) to this exact label from config, or
- Find the option in `select[name="state"]` that contains the state name and matches the “latest” pattern (e.g. `(YYYY-....)`).

---

## 7. Edge cases to handle in automation

- **Empty first option in `ssn2`:** Use explicit value `"01"` … `"99"` when selecting, not index 0.
- **Rate limit / errors:** If `#wherewhen` shows “unexplained error” or “Please notify me”, back off (longer delay, possibly new session/IP).
- **Parsing “766 to 772”:** Split on ` to ` and parse min/max for the random area range.

This file can be used as the single reference when writing the Playwright automation script that implements the flow in `note.md`.
