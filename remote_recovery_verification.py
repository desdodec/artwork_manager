"""Read-only verification of R2 backup objects recorded in a publication journal.

No credentials, networking, restoration, uploads, deletes, Supabase or side effects
are performed by this module itself. Caller supplies an S3-compatible read client.
Only get_object and head_object are used. Validation fails closed before cloud reads.

This verifies that remote backups match the *recorded* audit digest. It does NOT
establish that the journal is trustworthy, the current originals are unchanged,
or that any database rollback would be safe.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from botocore.exceptions import ClientError
from publisher import DATASET
from recovery_assessment import assess_recovery

HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
UUID4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
BACKUP_ROOT = DATASET + "/_publication_backups/"
READ_CHUNK = 1024 * 1024


def _safe_relative(path: str) -> bool:
    return (
        bool(path) and not path.startswith("/")
        and not path.endswith("/") and "\\" not in path
        and all(component not in ("", ".", "..") for component in path.split("/"))
    )


def _validate_journal(audit: dict) -> tuple[str, list[dict]]:
    """Validate ALL entries before doing any network work."""
    if not isinstance(audit, dict):
        raise ValueError("Audit must be a JSON object")
    operation_id = audit.get("operation_id")
    if not isinstance(operation_id, str) or not UUID4.fullmatch(operation_id):
        raise ValueError("Operation ID must be a valid UUID4")
    if not isinstance(audit.get("backups"), list) or not isinstance(audit.get("files"), list):
        raise ValueError("Audit requires backups and files lists")
    expected_root = BACKUP_ROOT + operation_id + "/"
    seen_original = set()
    seen_backup = set()
    validated = []
    for record in audit["backups"]:
        if not isinstance(record, dict):
            raise ValueError("Malformed backup entry")
        original = record.get("original_key")
        backup = record.get("backup_key")
        sha = record.get("sha256")
        size = record.get("bytes")
        if not isinstance(original, str) or not original.startswith(DATASET + "/"):
            raise ValueError("Backup original key outside dataset")
        relative = original[len(DATASET) + 1:]
        if not _safe_relative(relative) or relative.startswith("_publication_backups/"):
            raise ValueError("Unsafe original key")
        if backup != expected_root + relative:
            raise ValueError("Backup key does not match protected operation prefix and original")
        if not isinstance(sha, str) or not HEX_SHA256.fullmatch(sha):
            raise ValueError("Missing or invalid recorded SHA-256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("Missing or invalid recorded byte count")
        if original in seen_original or backup in seen_backup:
            raise ValueError("Duplicate backup key in journal")
        seen_original.add(original)
        seen_backup.add(backup)
        validated.append({
            "original_key": original, "backup_key": backup,
            "sha256": sha.lower(), "bytes": size,
        })

    # Don't claim coverage if the journal is incomplete or internally inconsistent.
    if audit.get("backup_state") != "complete":
        raise ValueError("Full backup phase not recorded complete")
    total = audit.get("backup_total")
    if not isinstance(total, int) or isinstance(total, bool) or total != len(validated):
        raise ValueError("Backup count inconsistent with journal")
    for entry in audit["files"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
            raise ValueError("Malformed file entry")
        if entry.get("status") == "REPLACE" and entry["key"] not in seen_original:
            raise ValueError("Completed replacement has no backup record")
    interrupted = audit.get("overwrite_in_progress")
    if interrupted is not None:
        if not isinstance(interrupted, dict) or interrupted.get("original_key") not in seen_original:
            raise ValueError("Interrupted replacement has no backup record")
        matching = next(b for b in validated if b["original_key"] == interrupted["original_key"])
        if interrupted.get("backup_key") != matching["backup_key"]:
            raise ValueError("Interrupted replacement references unexpected backup")
    for record in audit.get("restore_needed", []):
        if not isinstance(record, dict) or record.get("original_key") not in seen_original:
            raise ValueError("Restore record has no verified backup in journal")
        matching = next(b for b in validated if b["original_key"] == record["original_key"])
        if record.get("backup_key") != matching["backup_key"]:
            raise ValueError("Restore record references unexpected backup")
    return operation_id, validated


def verify_remote_backups(audit_path: Path, client, bucket: str) -> dict:
    """Verify actual R2 backup bytes without mutating R2 or Supabase.

    Returns partial-failure reports rather than silently skipping missing objects.
    Does not read originals. Never performs a restore.
    """
    if not isinstance(bucket, str) or not bucket or "/" in bucket or "\\" in bucket:
        raise ValueError("Invalid bucket name")
    path = Path(audit_path)
    audit = json.loads(path.read_text(encoding="utf-8"))
    operation_id, records = _validate_journal(audit)
    assessment = assess_recovery(path)
    if assessment["concerns"]:
        raise ValueError("Journal recovery assessment has warnings: " + "; ".join(assessment["concerns"][:3]))

    checks = []
    for record in records:
        key = record["backup_key"]
        expected_sha = record["sha256"]
        expected_size = record["bytes"]
        status = "error"
        actual_sha = None
        actual_size = None
        detail = None
        try:
            head = client.head_object(Bucket=bucket, Key=key)
            if head.get("ContentLength") != expected_size:
                status = "size_mismatch"
                actual_size = head.get("ContentLength")
            else:
                stream = client.get_object(Bucket=bucket, Key=key)["Body"]
                digest = hashlib.sha256()
                actual_size = 0
                try:
                    for block in iter(lambda: stream.read(READ_CHUNK), b""):
                        digest.update(block)
                        actual_size += len(block)
                finally:
                    stream.close()
                actual_sha = digest.hexdigest()
                if actual_size != expected_size:
                    status = "size_mismatch"
                elif actual_sha != expected_sha:
                    status = "sha256_mismatch"
                else:
                    status = "verified"
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            status = "missing" if code in ("404", "NoSuchKey", "NotFound") else "error"
            detail = "R2 returned " + (code if code.isalnum() else "an error")
        except Exception as exc:
            detail = type(exc).__name__
        checks.append({
            "original_key": record["original_key"], "backup_key": key,
            "status": status, "expected_sha256": expected_sha,
            "actual_sha256": actual_sha, "expected_bytes": expected_size,
            "actual_bytes": actual_size, "detail": detail,
        })
    all_verified = bool(records) and len(checks) == len(records) and all(
        row["status"] == "verified" for row in checks
    )
    return {
        "operation_id": operation_id,
        "bucket": bucket, "backup_count": len(records),
        "verified_count": sum(row["status"] == "verified" for row in checks),
        "all_recorded_backups_verified": all_verified,
        "checks": checks, "read_only": True,
        "supabase_checked": False, "original_objects_checked": False,
        "restoration_authorized": False,
        "warning": "Matching audit digests alone do not establish safe restoration or publication status.",
    }


def format_remote_verification(result: dict) -> str:
    lines = [
        "READ-ONLY R2 BACKUP VERIFICATION",
        "Operation: " + result["operation_id"],
        "Bucket: " + result["bucket"],
        "Backups verified: " + str(result["verified_count"]) + "/" + str(result["backup_count"]),
        "All recorded backups verified: " + ("YES" if result["all_recorded_backups_verified"] else "NO"),
        "Supabase and current original objects: NOT CHECKED",
        "Restoration: NOT AUTHORISED",
        result["warning"],
    ]
    for record in result["checks"]:
        if record["status"] != "verified":
            lines.append("ISSUE: " + record["status"] + " " + record["backup_key"])
    return "\n".join(lines)
