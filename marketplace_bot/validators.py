"""Validate order form fields. Each function returns (is_valid: bool, error_message: str | None)."""

from __future__ import annotations

import re
from datetime import datetime

from marketplace_bot.profiles import STATE_ABBR_TO_FULL

# Name: letters, spaces, hyphens, apostrophes; no digits; 1–50 chars
_NAME_RE = re.compile(r"^[a-zA-Z\s\-']{1,50}$")

# Email: basic pattern
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

# City: letters, spaces, hyphens, apostrophes; 1–50 chars
_CITY_RE = re.compile(r"^[a-zA-Z\s\-']{1,50}$")

# ZIP: 5 digits, or 5+4 with optional dash, or 9 consecutive digits
_ZIP_RE = re.compile(r"^(\d{5}(-\d{4})?|\d{9})$")

# DOB: MM/DD/YYYY
_DOB_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def validate_first_name(text: str) -> tuple[bool, str | None]:
    s = (text or "").strip()
    if not s:
        return False, "First name cannot be empty."
    if any(c.isdigit() for c in s):
        return False, "First name must contain only letters (no numbers)."
    if len(s) > 50:
        return False, "First name is too long (max 50 characters)."
    if not _NAME_RE.match(s):
        return False, "First name can only contain letters, spaces, hyphens, or apostrophes (e.g. Mary-Jane)."
    return True, None


def validate_last_name(text: str) -> tuple[bool, str | None]:
    s = (text or "").strip()
    if not s:
        return False, "Last name cannot be empty."
    if any(c.isdigit() for c in s):
        return False, "Last name must contain only letters (no numbers)."
    if len(s) > 50:
        return False, "Last name is too long (max 50 characters)."
    if not _NAME_RE.match(s):
        return False, "Last name can only contain letters, spaces, hyphens, or apostrophes."
    return True, None


def validate_email(text: str) -> tuple[bool, str | None]:
    s = (text or "").strip()
    if not s:
        return False, "Email cannot be empty."
    if len(s) > 254:
        return False, "Email is too long."
    if " " in s:
        return False, "Email must not contain spaces."
    if not _EMAIL_RE.match(s):
        return False, "Please enter a valid email (e.g. name@example.com)."
    return True, None


def validate_phone(text: str) -> tuple[bool, str | None]:
    s = (text or "").strip()
    digits = "".join(c for c in s if c.isdigit())
    if len(digits) < 10:
        return False, "Phone must have at least 10 digits (e.g. 312-555-1234)."
    if len(digits) > 15:
        return False, "Phone has too many digits."
    if len(digits) == 11 and digits.startswith("1"):
        return True, None  # 1-XXX-XXX-XXXX
    if len(digits) == 10:
        return True, None
    return False, "Please enter a valid US phone (10 digits, with or without 1)."


def validate_street(text: str) -> tuple[bool, str | None]:
    s = (text or "").strip()
    if not s:
        return False, "Street address cannot be empty."
    if len(s) < 5:
        return False, "Street address is too short (e.g. 123 Main St)."
    if len(s) > 100:
        return False, "Street address is too long (max 100 characters)."
    # Allow letters, numbers, spaces, comma, period, #, -, '
    if not re.match(r"^[a-zA-Z0-9\s,.#'\-]+$", s):
        return False, "Street can only contain letters, numbers, spaces, and basic punctuation (e.g. 123 Main St, Apt 4)."
    return True, None


def validate_city(text: str) -> tuple[bool, str | None]:
    s = (text or "").strip()
    if not s:
        return False, "City cannot be empty."
    if any(c.isdigit() for c in s):
        return False, "City must contain only letters (no numbers)."
    if len(s) > 50:
        return False, "City name is too long (max 50 characters)."
    if not _CITY_RE.match(s):
        return False, "City can only contain letters, spaces, hyphens, or apostrophes."
    return True, None


def validate_state(text: str) -> tuple[bool, str | None]:
    s = (text or "").strip().upper()
    if not s:
        return False, "State cannot be empty."
    if len(s) != 2:
        return False, "State must be exactly 2 letters (e.g. TX, FL, CA)."
    if not s.isalpha():
        return False, "State must be 2 letters only (no numbers or symbols)."
    if s not in STATE_ABBR_TO_FULL:
        return False, f"'{s}' is not a valid US state code. Use 2 letters (e.g. TX, NY, CA)."
    return True, None


def validate_zip(text: str) -> tuple[bool, str | None]:
    s = (text or "").strip()
    # Allow "12345" or "12345-6789"
    if not s:
        return False, "ZIP code cannot be empty."
    # Remove spaces
    s = s.replace(" ", "")
    if not _ZIP_RE.match(s):
        return False, "ZIP must be 5 digits, or 5+4 with or without dash (e.g. 77864 or 77864-1234)."
    return True, None


def validate_date_of_birth(text: str) -> tuple[bool, str | None]:
    s = (text or "").strip()
    if not s:
        return False, "Date of birth cannot be empty."
    m = _DOB_RE.match(s)
    if not m:
        return False, "Use format MM/DD/YYYY (e.g. 01/15/1990)."
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if month < 1 or month > 12:
        return False, "Month must be between 01 and 12."
    if day < 1 or day > 31:
        return False, "Day must be between 01 and 31."
    if year < 1900 or year > 2010:
        return False, "Year must be between 1900 and 2010."
    try:
        dt = datetime(year, month, day)
    except ValueError:
        return False, "That date is invalid (e.g. Feb 30 doesn't exist)."
    if dt > datetime.now():
        return False, "Date of birth cannot be in the future."
    return True, None
