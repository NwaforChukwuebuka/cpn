"""
Module E — Capital One application filler.

Fills the 8-step Capital One application from profile and steps config.
Concurrent execution: use run_filler_from_data() or run_filler_async() with
in-memory profile and steps_config; each session uses its own adspower_profile.

Run from CLI: python -m modules.capital_one
(avoids RuntimeWarning that occurs with python -m modules.capital_one.run_filler)
"""


def __getattr__(name: str):
    """Lazy import so run_filler is not loaded when package is imported.
    This avoids the RuntimeWarning when running python -m modules.capital_one.run_filler.
    """
    if name in ("get_profile_value", "load_json", "run_filler", "run_filler_async", "run_filler_from_data"):
        from .run_filler import (
            get_profile_value,
            load_json,
            run_filler,
            run_filler_async,
            run_filler_from_data,
        )
        return {
            "get_profile_value": get_profile_value,
            "load_json": load_json,
            "run_filler": run_filler,
            "run_filler_async": run_filler_async,
            "run_filler_from_data": run_filler_from_data,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "get_profile_value",
    "load_json",
    "run_filler",
    "run_filler_async",
    "run_filler_from_data",
]
