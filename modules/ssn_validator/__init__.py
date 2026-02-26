"""
Module B — SSN Validator / Deceased Check (Step 3).

Reads partial_cpn.json (from Module A), completes the last 4 digits,
and checks SSA Death Masterfile + issuance status at https://www.ssn-verify.com/.
Uses an AdsPower browser profile for automation.
"""

from .run_validator import run

__all__ = ["run"]
