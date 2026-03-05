"""
Module B — SSN Validator / Deceased Check (Step 3).

Reads partial CPN data (dict or file), completes the last 4 digits,
and checks SSA Death Masterfile + issuance status at https://www.ssn-verify.com/.
Uses an AdsPower browser profile for automation.

Concurrent execution: use run_validation() or run_validation_async() with
per-session partial_cpn and adspower_profile (no global state).
"""

from .run_validator import normalize_partial, run, run_validation, run_validation_async

__all__ = ["normalize_partial", "run", "run_validation", "run_validation_async"]
