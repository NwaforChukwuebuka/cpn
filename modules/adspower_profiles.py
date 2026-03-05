"""
Shared list of AdsPower browser profile IDs for use by Capital One, First Premier,
SSN Validator, and List Yourself modules. Use one profile per concurrent session.
"""

# Profile IDs to use for any module that runs via AdsPower (single source of truth).
ADSPOWER_PROFILES = [
    "k19jxstf",
    "k19jxste",
    "k19jxstd",
    "k19jxstc",
    "k19jxstb",
    "k19jxsta",
    "k19jxst9",
    "k19jxst8",
    "k19jxst7",
    "k19jxst6",
]

# Default profile (first in list) when no specific profile is passed.
DEFAULT_ADSPOWER_PROFILE = ADSPOWER_PROFILES[0]


def get_profile_for_index(index: int) -> str:
    """Return the profile ID for a given 0-based index (round-robin over the list)."""
    return ADSPOWER_PROFILES[index % len(ADSPOWER_PROFILES)]
