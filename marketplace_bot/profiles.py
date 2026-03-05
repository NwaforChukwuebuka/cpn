from __future__ import annotations

from typing import Any

# US state 2-letter code -> full name (for SSN/CPN workflow lookup)
STATE_ABBR_TO_FULL: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}


def workflow_state_from_profile(profile: dict[str, Any], fallback: str) -> str:
    """Resolve workflow state from buyer profile (address.state = 2-letter) with fallback."""
    address = profile.get("address") or {}
    abbr = (address.get("state") or "").strip().upper()
    if abbr and abbr in STATE_ABBR_TO_FULL:
        return STATE_ABBR_TO_FULL[abbr]
    # If they typed full name (e.g. Florida), use as-is if non-empty
    if abbr and len(abbr) > 2:
        return (address.get("state") or "").strip()
    return fallback


REQUIRED_FIELDS = (
    "first_name",
    "last_name",
    "email",
    "phone",
    "date_of_birth",
)


def merge_profile(default_template: dict[str, Any], user_profile: dict[str, Any]) -> dict[str, Any]:
    profile = dict(default_template)
    address = dict(default_template.get("address", {}))
    address.update(user_profile.get("address", {}))
    profile.update(user_profile)
    profile["address"] = address
    return profile


def validate_profile_input(profile: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_FIELDS:
        if not str(profile.get(field, "")).strip():
            errors.append(f"Missing required field: {field}")

    address = profile.get("address")
    if not isinstance(address, dict):
        errors.append("Address must be an object")
    else:
        for field in ("street", "city", "state", "zip", "country"):
            if not str(address.get(field, "")).strip():
                errors.append(f"Missing address field: {field}")
    return errors
