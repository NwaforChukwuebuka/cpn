# CPN Number Guide — Step-by-Step

CPN numbers are generated to match SSN formatting rules. They must be validated at each stage. Follow these steps in order.

---

## Phase 1: Generate the CPN Number

### Step 1 — Get the first 3 digits (area number)

**Goal:** Get a valid 3-digit prefix range for your chosen state.

1. Open: https://stevemorse.org/ssn/ssn.html
2. In the **first section** (“Three-Digit Decoder”):
   - Use the **“was issued in”** dropdown (where the card was issued, not birth state).
   - For your state, choose the option with the **latest date range**.
   - If one option ends in “….” (e.g. “2001–….”), pick that. Otherwise pick the latest end date.
   - Note the **“SSN starting with”** range shown (e.g. **766 to 772** for Florida with latest date).

**Example:** Florida, latest date → first prefix range **766 to 772**.

**Important:** Each state can have several date ranges. Always use the one with the latest date (or “….”).

---

### Step 2 — Get the next 2 digits (group number)

**Goal:** Find a 2-digit group that returns “Not Issued.”

1. Stay on: http://stevemorse.org/ssn/ssn.html  
2. In the **second section**:
   - In **“SSN Starting with”**, pick one value from your range (e.g. **772**).
   - On the right, try **2-digit group** values until the result is **“Not Issued.”**
   - Example: group **11** → “Not Issued” → partial CPN is **772-11-XXXX**.

**Rate limit:** Do not search more than ~3 numbers per minute. Search in random order, not sequentially, to avoid IP blocks. If blocked, wait or use another device/network and clear cookies/cache.

---

### Step 3 — Complete the 9-digit number and check deceased file

**Goal:** Add the last 4 digits and confirm the number is not on the death file.

1. Open: https://www.ssn-verify.com/ 
2. Enter your **partial CPN** (e.g. 772-11-XXXX).
3. Choose **any 4 digits** for the last segment (e.g. **5245**). Full CPN example: **772-11-5245**.
4. Submit and check the result. You want:
   - **SSA Death Masterfile:** “No record” (not deceased).
   - **Status:** Indicates not issued / not in use (e.g. “has NOT been issued” or similar).  
   Note: SSNValidator may not verify numbers issued after SSA randomization (June 25, 2011); that’s expected for newer ranges.

**Write down the full CPN.** You will use it in the next step and for the profile.

---

### Step 4 — Confirm number is not in use (InstantID-style check)

**Goal:** Ensure the CPN shows as “Reserved for future use” or “Inactive,” not issued to someone else.

1. Open: https://www.searchbug.com/peoplefinder/verify-ssn-free.aspx  
   (Or another InstantID-style verification service.)
2. Enter your **full CPN** (e.g. 772-11-5245) in the search box.
3. Check the result:
   - **Accept:** Status = **“Reserved for future use”** or **“Inactive.”** State/Year may show (e.g. Florida, Reserved for future use).
   - **Reject:** Status = “Issued” or any indication it belongs to a deceased person. If so, **discard this number and go back to Step 2** (pick a different group or area).

SearchBug offers 3 free scans; other services may have different limits or fees.

---

## Phase 2: Set Up the Profile

Use **one consistent set** of details for this CPN. Do not link them to your real identity.

### Step 5 — Address

- Use an address where you have **never** had mail or bills in your real name.
- You must be able to **receive mail** there.
- Do not use an address too close to your real address (to avoid file mixing).

### Step 6 — Phone

- Use a number **not linked** to you (e.g. Google Voice, TextFree+, or similar).
- Same rule: no link to your real identity to avoid mixing files.

### Step 7 — Email

- Use an email **not linked** to you (e.g. new Gmail, Yahoo, etc.).

### Step 8 — Application details (use exactly these ranges)

- **Annual income:** $50,000–$80,000  
- **Time at current address:** 5 years 5 months  
- **Type of job:** Self Employed  
- **Time on job:** 5 years 5 months  

Use the **same** values every time you apply with this CPN.

---

## Phase 3: Tri-Merge and Public Records

### Step 9 — Tri-merge (create credit file)

- **Goal:** Get your name, address, and CPN on file at the three major bureaus.
- Apply for 1–2 cards from **different** banks (e.g. Capital One and a sub-prime such as First Premier).
- Expect to be **declined**. The purpose is only to create the bureau file.
- Wait **at least 2 business days** before applying for other offers.

### Step 10 — Public records listing

- **Wait at least 48 hours** after Step 9.
- Open: https://www.listyourself.net/ListYourself/listing.jsp  
- Submit the **same** CPN profile: name, new address, phone, etc., so this data appears in public records and matches what lenders may see.

### Step 11 — Verify with CreditKarma

- Go to creditkarma.com and sign up using the **exact** CPN profile (name, address, etc.) from the tri-merge step.
- If setup is correct and the CPN is not in use by someone else, you should be able to open the account and see at least one inquiry from Step 9.
- If you cannot sign up, get errors, or the account data does not match your tri-merge info: **stop**. The CPN may not be valid for you or may already be in use; start over with new CPN and new profile.

---

## Summary checklist

| Phase | Step | Action |
|-------|------|--------|
| 1     | 1    | Steve Morse: get 3-digit range for state (latest date) |
| 1     | 2    | Steve Morse: get 2-digit group “Not Issued” → partial CPN |
| 1     | 3    | SSNValidator: add last 4 digits, confirm not deceased |
| 1     | 4    | SearchBug (or similar): confirm Status = Reserved/Inactive |
| 2     | 5–8  | Profile: address, phone, email, income/job details |
| 3     | 9    | Tri-merge: 1–2 card applications, then wait 2+ business days |
| 3     | 10   | ListYourself: public listing (after 48+ hours) |
| 3     | 11   | CreditKarma: sign up and verify file matches |

---

## FAQ (condensed)

- **Steve Morse “unexplained error” / block:** Rate limit (~3 searches/minute). Use random, non-sequential searches. If blocked, wait or use another device/network and clear cookies/cache. Third block can be permanent.
- **No “Not Issued” for my state:** Always pick the **latest** date range in Step 1 and try the **full** prefix range (e.g. 667–675 for Georgia). Try random area + group combinations. If still none, use another state (e.g. birth state or neighboring state).
