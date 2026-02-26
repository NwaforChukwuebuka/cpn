# Flow: Get the First Five Digits of a CPN

**AIM:** Obtain a valid 5-digit prefix (area + group) for a CPN, i.e. the first five digits in the form `AAA-GG-XXXX`.

---

## 1. Customer chooses state

- Example: customer chooses **Florida (FL)**.
- We will use the **latest** “was issued in” range for that state (e.g. Florida 2001–...., not 1936–1980 or 1980–2001).

---

## 2. Verify the first three digits (area) range — two sources

We need **two ways** of verifying the valid 3-digit area range for the chosen state:

### 2a. Config / data file

- Use the **config** (e.g. `state_area_ranges.json`) that lists each state’s area ranges.
- For the chosen state, take only the **latest** issuance period (e.g. Florida (2001-....) → range **766–772**).
- Treat this as the authoritative 3-digit range for “SSN starting with” for that state.

### 2b. Live verification on the website

- Open the **Three-Digit Decoder** on the actual Steve Morse site.
- Select the **latest** option for that state (e.g. “Florida (2001-....)”).
- Confirm that the “SSN starting with” / “was issued in” display matches the **same first three digits range** as in the config (e.g. 766 to 772 for Florida).
- If config and website disagree, resolve (e.g. prefer website and update config).

Once both agree, the **verified 3-digit area range** is fixed (e.g. [766, 772] for Florida).

---

## 3. Generate a random 3-digit area in the verified range

- From the verified range (e.g. 766–772), generate a **random 3-digit area** (e.g. 769).
- This is the first three digits of the CPN (the “area” in AAA-GG-XXXX).

---

## 4. Get the remaining 2 digits (group) via the Five-Digit Decoder

- We need **5 digits total**: 3 (area) + 2 (group).
- Use the **Five-Digit Decoder** on the site:
  - **First dropdown (SSN starting with):** set to our chosen **3-digit area** (e.g. 769).
  - **Second dropdown:** 2-digit **group** (01–99). We do not know a valid group yet.
- **Loop:**
  1. Pick a **random 2-digit group** (01–99).
  2. Set the first dropdown to our 3-digit area and the second to this group.
  3. Wait for the page to load (e.g. “was issued in” / `#wherewhen` or equivalent :  E.g: SSN starting with	was issued in	766-32: Florida, 2004).
  4. Read the result:
     - If it says something like **“not issued”** (or equivalent “this prefix has not been issued”) → **this 5-digit prefix (area + group) is valid.** Stop and use it.
     - If it shows a place/date (e.g. “Florida, 2005–2010”) → this prefix is already issued; try another random 2-digit group.
  5. Repeat until we get “not issued” for some group (or hit a safety limit).
- **Output:** First five digits = **AAA + GG** (e.g. 769-42 if 42 was the group that returned “not issued”).

---

## 5. Summary

| Step | What we do |
|------|-------------|
| 1 | Customer picks state (e.g. FL). Use **latest** issuance period for that state. |
| 2 | **Verify** the 3-digit area range in two ways: (a) config/data file, (b) Three-Digit Decoder on the live site. Confirm they match. |
| 3 | **Generate** a random 3-digit area within that verified range. |
| 4 | On the **Five-Digit Decoder**: fix the first dropdown to that area; try **random 2-digit groups** until the site shows **“not issued”**; then the first five digits = area + that group. |

**Note:** The Five-Digit Decoder may rate-limit or show errors (e.g. “unexplained error” or “Please notify me”). If that happens, use IP rotation/VPN and retry with appropriate delays (e.g. 20+ seconds between requests) to avoid bans.
