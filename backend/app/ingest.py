from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import httpx
from psycopg.types.json import Jsonb

from .config import Settings
from .db import connection

VEHICLE_QUERIES = [
    ("Ford", "F-150", 2020),
    ("Honda", "Civic", 2021),
    ("Toyota", "Camry", 2022),
    ("Chevrolet", "Silverado 1500", 2021),
    ("Tesla", "Model 3", 2022),
]


def _report_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:10], fmt).date()
        except ValueError:
            continue
    return None


def _normalize(item: dict[str, Any], make: str, model: str, year: int) -> dict[str, Any]:
    campaign_id = str(item.get("NHTSACampaignNumber") or "").upper()
    normalized_make = str(item.get("Make") or make).upper()
    normalized_model = str(item.get("Model") or model).upper()
    normalized_year = int(item.get("ModelYear") or year)
    return {
        "source_key": f"nhtsa:{campaign_id}:{normalized_make}:{normalized_model}:{normalized_year}",
        "campaign_id": campaign_id,
        "manufacturer": str(item.get("Manufacturer") or make),
        "component": str(item.get("Component") or "UNKNOWN"),
        "summary": str(item.get("Summary") or ""),
        "consequence": str(item.get("Consequence") or ""),
        "remedy": str(item.get("Remedy") or ""),
        "park_outside": bool(item.get("parkOutSide")),
        "do_not_drive": bool(item.get("doNotDrive") or item.get("parkIt")),
        "report_date": _report_date(item.get("ReportReceivedDate")),
        "make": normalized_make,
        "model": normalized_model,
        "model_year": normalized_year,
        "payload": item,
    }


def refresh_nhtsa(settings: Settings) -> dict[str, object]:
    """Incrementally upsert the small public vehicle set used by the portfolio demo."""
    refreshed_at = datetime.now(UTC)
    fetched = 0
    failures: list[str] = []
    records: list[dict[str, Any]] = []
    with httpx.Client(timeout=15, headers={"User-Agent": "Recall RAG/1.0"}) as client:
        for make, model, year in VEHICLE_QUERIES:
            try:
                response = client.get(
                    "https://api.nhtsa.gov/recalls/recallsByVehicle",
                    params={"make": make, "model": model, "modelYear": year},
                )
                response.raise_for_status()
                results = response.json().get("results", [])
                records.extend(_normalize(item, make, model, year) for item in results if item.get("NHTSACampaignNumber"))
                fetched += len(results)
            except Exception as error:  # A partial public refresh should not destroy existing records.
                failures.append(f"{make} {model} {year}: {error}")

    with connection(settings) as conn:
        for record in records:
            conn.execute(
                """INSERT INTO raw_nhtsa_recalls
                (source_key,campaign_id,manufacturer,component,summary,consequence,remedy,park_outside,do_not_drive,report_date,source_url,source_refreshed_at,raw_payload)
                VALUES (%(source_key)s,%(campaign_id)s,%(manufacturer)s,%(component)s,%(summary)s,%(consequence)s,%(remedy)s,%(park_outside)s,%(do_not_drive)s,%(report_date)s,%(source_url)s,%(refreshed_at)s,%(raw_payload)s)
                ON CONFLICT (source_key) DO UPDATE SET
                  manufacturer=excluded.manufacturer, component=excluded.component, summary=excluded.summary,
                  consequence=excluded.consequence, remedy=excluded.remedy, park_outside=excluded.park_outside,
                  do_not_drive=excluded.do_not_drive, report_date=excluded.report_date,
                  source_refreshed_at=excluded.source_refreshed_at, raw_payload=excluded.raw_payload""",
                {**record, "source_url": "https://api.nhtsa.gov/recalls/recallsByVehicle", "refreshed_at": refreshed_at, "raw_payload": Jsonb(record["payload"])},
            )
            conn.execute(
                """INSERT INTO raw_nhtsa_recall_vehicles (source_key,campaign_id,make,model,model_year,source_refreshed_at)
                VALUES (%(source_key)s,%(campaign_id)s,%(make)s,%(model)s,%(model_year)s,%(refreshed_at)s)
                ON CONFLICT (source_key) DO UPDATE SET source_refreshed_at=excluded.source_refreshed_at""",
                {**record, "refreshed_at": refreshed_at},
            )
        conn.commit()
    return {"fetched": fetched, "upserted": len(records), "failures": failures, "refreshed_at": refreshed_at.isoformat()}
