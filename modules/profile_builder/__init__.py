"""
Module D — Profile Builder (Steps 5–8).

Builds profile from template and optional full_cpn / verification data.
Concurrent execution: use build_profile_from_data() or build_profile_async()
with in-memory dicts per session; no global state.
"""

from .build import (
    build_profile,
    build_profile_async,
    build_profile_from_data,
    format_address_line,
    load_json,
    profile_for_output,
    validate_income,
    validate_job_type,
    validate_times,
)

__all__ = [
    "build_profile",
    "build_profile_async",
    "build_profile_from_data",
    "format_address_line",
    "load_json",
    "profile_for_output",
    "validate_income",
    "validate_job_type",
    "validate_times",
]
