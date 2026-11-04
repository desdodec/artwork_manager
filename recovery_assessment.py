"""Read-only assessment of local Street Face publication recovery journals.

This module does NOT contact R2 or Supabase, and cannot restore or delete objects.
A backed-up object recorded here was verified at the time of publication; its
CURRENT remote existence or integrity must still be checked before recovery.
"""
from __future__ import annotations

import json
from pathlib import Path

from publisher import DATASET


def assess_recovery(audit_path: Path) -> dict:
    path = Path(audit_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Publication audit must be a JSON object")
    operation_id = data.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("Audit lacks operation_id")
    files = data.get("files")
    backups = data.get("backups")
    if not isinstance(files, list) or not isinstance(backups, list):
        raise ValueError("Audit must contain files and backups lists")

    concerns = []
    backup_by_original = {}
    for b in backups:
        if not isinstance(b, dict):
            concerns.append("Malformed backup entry")
            continue
        original, backup = b.get("original_key"), b.get("backup_key")
        if not isinstance(original, str) or not isinstance(backup, str):
            concerns.append("Backup lacks object key")
            continue
        if original in backup_by_original:
            concerns.append("Duplicate backup record: " + original)
        if not original.startswith(DATASET + "/"):
            concerns.append("Unexpected original key: " + original)
        if not backup.startswith(DATASET + "/_publication_backups/" + operation_id + "/"):
            concerns.append("Unexpected backup key: " + backup)
        backup_by_original[original] = b

    completed = set()
    potential = set()
    new_objects = set()
    for row in files:
        if not isinstance(row, dict) or not isinstance(row.get("key"), str):
            concerns.append("Malformed completed-file entry")
            continue
        key = row["key"]
        action = row.get("status")
        if action == "REPLACE":
            completed.add(key)
            potential.add(key)
        elif action == "NEW":
            new_objects.add(key)
        elif action != "SAME":
            concerns.append("Unknown completed-file action for " + key)

    interrupted = data.get("overwrite_in_progress")
    interrupted_key = None
    if interrupted is not None:
        if isinstance(interrupted, dict) and isinstance(interrupted.get("original_key"), str):
            interrupted_key = interrupted["original_key"]
            potential.add(interrupted_key)
        else:
            concerns.append("Malformed interrupted-overwrite entry")

    uncertain_new = set()
    uncertain_replacements = set()
    uncertain_unknown = set()
    upload = data.get("upload_in_progress")
    if upload is not None:
        if not isinstance(upload, dict) or not isinstance(upload.get("key"), str):
            concerns.append("Malformed interrupted-upload entry; remote outcome unknown")
        else:
            key = upload["key"]
            action = upload.get("action")
            if action == "REPLACE" or key == interrupted_key or key in backup_by_original:
                uncertain_replacements.add(key)
                potential.add(key)
            elif action == "NEW":
                uncertain_new.add(key)
            else:
                uncertain_unknown.add(key)
                concerns.append("Interrupted upload type unknown: " + key)

    # A save or process can fail after an overwrite and before its final journal
    # update. Without a definitive remote state, completed backups are all
    # considered potentially affected, not automatically safe to restore.
    restore_records = data.get("restore_needed", [])
    if not isinstance(restore_records, list):
        concerns.append("Malformed restore_needed list")
        restore_records = []
    for r in restore_records:
        if isinstance(r, dict) and isinstance(r.get("original_key"), str):
            potential.add(r["original_key"])
        else:
            concerns.append("Malformed restore_needed entry")

    missing = sorted(potential - backup_by_original.keys())
    for key in missing:
        concerns.append("No recorded verified backup for potentially changed object: " + key)
    if data.get("backup_state") != "complete" and potential:
        concerns.append("Potential overwrites exist but full backup phase not recorded complete")

    reported_total = data.get("backup_total")
    if isinstance(reported_total, int) and reported_total != len(backups) and data.get("backup_state") == "complete":
        concerns.append("Full backup count does not match verified backup records")

    artwork_only = data.get("action") == "upload_artwork_only"
    if artwork_only and data.get("artwork_complete"):
        state = "artwork_upload_reported"
        advice = "Audit reports artwork upload only; no catalogue publication was requested. Verify current remote state independently."
    elif data.get("complete"):
        state = "published_reported"
        advice = "Audit reports successful database publication; do not restore without independent catalogue and R2 verification."
    elif potential or new_objects or upload is not None:
        state = "manual_reconciliation_required"
        advice = "Audit indicates potential R2 modifications; verify actual R2 and Supabase state before any manual restoration."
    elif data.get("backup_state") == "complete":
        state = "backups_only"
        advice = "Backups were recorded without any recorded object replacements. Check R2 before taking action."
    else:
        state = "no_recorded_overwrites"
        advice = "No object replacement is recorded. An incomplete journal cannot prove remote objects are unchanged."

    return {
        "operation_id": operation_id,
        "audit_file": str(path),
        "state": state,
        "publication_complete_reported": bool(data.get("complete")) and not artwork_only,
        "backup_state": data.get("backup_state", "unknown"),
        "backups_recorded": len(backups),
        "backups_expected": reported_total,
        "completed_replacements": len(completed),
        "interrupted_replacement": interrupted_key,
        "artwork_upload_complete_reported": artwork_only and bool(data.get("artwork_complete")),
        "uncertain_new_keys": sorted(uncertain_new),
        "uncertain_replacement_keys": sorted(uncertain_replacements),
        "uncertain_upload_keys": sorted(uncertain_unknown),
        "new_objects_recorded": len(new_objects),
        "potentially_changed_keys": sorted(potential),
        "missing_backup_keys": missing,
        "concerns": concerns,
        "advice": advice,
        "remote_verified_now": False,
    }


def format_assessment(result: dict) -> str:
    lines = [
        "READ-ONLY RECOVERY ASSESSMENT",
        "Operation: " + result["operation_id"],
        "State: " + result["state"],
        "Backup phase: " + str(result["backup_state"]),
        "Backup records: " + str(result["backups_recorded"])
            + " / " + str(result["backups_expected"]),
        "Completed replacements in audit: " + str(result["completed_replacements"]),
        "Interrupted replacement: " + str(result["interrupted_replacement"] or "none"),
        "New objects recorded: " + str(result["new_objects_recorded"]),
        "Potentially changed objects: " + str(len(result["potentially_changed_keys"])),
        "Potentially changed objects without backup record: " + str(len(result["missing_backup_keys"])),
        "Uncertain NEW uploads: " + str(result["uncertain_new_keys"]),
        "Uncertain replacements: " + str(result["uncertain_replacement_keys"]),
        "Uncertain uploads of unknown type: " + str(result["uncertain_upload_keys"]),
        "Current R2 backup existence/integrity: NOT CHECKED",
        "Current Supabase publication status: NOT CHECKED",
        result["advice"],
    ]
    for c in result["concerns"][:20]:
        lines.append("WARNING: " + c)
    if len(result["concerns"]) > 20:
        lines.append("Additional warnings: " + str(len(result["concerns"]) - 20))
    return "\n".join(lines)
