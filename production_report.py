"""Read generator publication selection, without making any network changes.

The generator report is authoritative for why an artwork was included or skipped.
The street_key in each written street is the permanent identifier. Exclusion
entries may lack a street_key; a publisher must resolve them from the geographic
catalogue or compare against the selected keys in an explicitly complete release.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


class ProductionReportError(ValueError):
    """An inconsistent or unsafe generator production report."""


def read_production_report(path: str | Path) -> dict:
    document = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict):
        raise ProductionReportError("Expected an object")
    processed = document.get("processed")
    written = document.get("written")
    skipped = document.get("skipped")
    streets = document.get("streets")
    if not isinstance(processed, int) or processed < 0:
        raise ProductionReportError("Missing processed count")
    if not isinstance(written, int) or written < 0:
        raise ProductionReportError("Missing written count")
    if not isinstance(skipped, list) or not isinstance(streets, list):
        raise ProductionReportError("Missing skipped or streets list")
    if written != len(streets) or processed != written + len(skipped):
        raise ProductionReportError(
            f"Counts inconsistent: processed={processed}, written={written}, "
            f"skipped={len(skipped)}, streets={len(streets)}"
        )
    keys = []
    for position, entry in enumerate(streets):
        if not isinstance(entry, dict):
            raise ProductionReportError(f"Street {position}: invalid record")
        location = entry.get("location") or {}
        key = location.get("street_key")
        if not isinstance(key, str) or not key.strip():
            raise ProductionReportError(f"Street {position}: missing street_key")
        if not entry.get("png") or not entry.get("svg") or not entry.get("mockup_front") or not entry.get("mockup_rear"):
            raise ProductionReportError(f"Street {position}: missing production file reference")
        if entry.get("status") == "fail":
            raise ProductionReportError(f"Street {position}: generator marked as failed")
        keys.append(key)
    duplicates = sorted(k for k, n in Counter(keys).items() if n > 1)
    if duplicates:
        raise ProductionReportError(f"Duplicate street_key(s): {duplicates[:5]}")
    for position, entry in enumerate(skipped):
        if not isinstance(entry, dict) or entry.get("status") != "not_for_production":
            raise ProductionReportError(f"Skipped item {position} lacks explicit not_for_production status")
        if not entry.get("item"):
            raise ProductionReportError(f"Skipped item {position} missing name")
    return {
        "processed": processed,
        "written": written,
        "excluded": len(skipped),
        "selected_keys": keys,
        "selected_streets": streets,
        "excluded_items": skipped,
        "warnings": sum(entry.get("status") == "warning" for entry in streets),
    }


def exclusion_preview(report: dict, currently_listed_keys: set[str], *, complete: bool) -> dict:
    """Compute exclusions by immutable key, *not* street name or row number.

    In a first release, there may be zero already-published keys, even though
    the generator excluded artwork from its original candidate list.
    """
    selected = set(report["selected_keys"])
    return {
        "selected_count": len(selected),
        "explicit_generator_exclusions": report["excluded"],
        "currently_listed": len(currently_listed_keys),
        "to_unlist": sorted(currently_listed_keys - selected) if complete else [],
        "release_mode": "complete" if complete else "partial",
    }
