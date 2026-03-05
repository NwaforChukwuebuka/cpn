from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any


def build_profile_csv_bytes(profile: dict[str, Any]) -> bytes:
    address = profile.get("address", {}) or {}
    out = io.StringIO()
    writer = csv.DictWriter(
        out,
        fieldnames=[
            "cpn",
            "first_name",
            "middle_initial",
            "last_name",
            "email",
            "phone",
            "date_of_birth",
            "street",
            "city",
            "state",
            "zip",
            "country",
            "annual_income",
            "job_type",
            "time_at_address",
            "time_on_job",
        ],
    )
    writer.writeheader()
    writer.writerow(
        {
            "cpn": profile.get("cpn", ""),
            "first_name": profile.get("first_name", ""),
            "middle_initial": profile.get("middle_initial", ""),
            "last_name": profile.get("last_name", ""),
            "email": profile.get("email", ""),
            "phone": profile.get("phone", ""),
            "date_of_birth": profile.get("date_of_birth", ""),
            "street": address.get("street", ""),
            "city": address.get("city", ""),
            "state": address.get("state", ""),
            "zip": address.get("zip", ""),
            "country": address.get("country", ""),
            "annual_income": profile.get("annual_income", ""),
            "job_type": profile.get("job_type", ""),
            "time_at_address": profile.get("time_at_address", ""),
            "time_on_job": profile.get("time_on_job", ""),
        }
    )
    return out.getvalue().encode("utf-8")


def persist_csv(order_id: str, csv_bytes: bytes, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    file_path = output_dir / f"{order_id}.csv"
    file_path.write_bytes(csv_bytes)
    return file_path
