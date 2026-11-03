"""Plan isolated R2 integration testing without accessing cloud resources.

This is deliberately a PLAN-ONLY harness. There are no R2 credentials,
network calls, upload/copy/delete/restore methods or --execute option.
A separate implementation and explicit approval are required for cloud writes.

Usage: python r2_isolated_test.py
       python r2_isolated_test.py --run-id 123e4567-e89b-42d3-a456-426614174000
"""
from __future__ import annotations

import argparse
import json
import re
import uuid

ROOT = "street-face-integration-tests/v1"
PRODUCTION = "street-face/hebden-bridge/v2/"
UUID4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def build_plan(run_id: str) -> dict:
    """Build an allowlisted disposable-object plan; no external side effects."""
    if not isinstance(run_id, str) or not UUID4.fullmatch(run_id):
        raise ValueError("run_id must be a valid version-4 UUID")
    run_id = str(uuid.UUID(run_id))
    prefix = ROOT + "/" + run_id + "/"
    objects = {
        "original": prefix + "live/original.png",
        "backup": prefix + "backups/original.png",
        "replacement": prefix + "live/replacement.png",
        "failure_probe": prefix + "live/failure_probe.png",
    }
    if any(not key.startswith(prefix) or key.startswith(PRODUCTION) for key in objects.values()):
        raise ValueError("Object key is outside isolated test prefix")
    if len(set(objects.values())) != len(objects):
        raise ValueError("Duplicate test destinations")
    return {
        "mode": "PLAN_ONLY",
        "run_id": run_id,
        "prefix": prefix,
        "objects": objects,
        "stages": [
            "Confirm test-only bucket, test-prefix and empty object keys",
            "Create small disposable original object",
            "Copy original to separate backup key",
            "Verify backup SHA-256 by reading stored bytes",
            "Replace disposable original and verify its digest",
            "Inject controlled upload failure and inspect recovery record",
            "Restore original from backup and verify digest",
            "Manually approve deletion of only run-owned test objects",
        ],
        "cloud_calls_performed": 0,
        "writes_allowed": False,
        "deletes_allowed": False,
        "production_prefix_forbidden": PRODUCTION,
        "note": (
            "No cloud operations exist in this script. Remote test implementation, "
            "bucket choice and each write/delete phase need separate review and approval."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", help="Optional UUID4 to make a repeatable test plan")
    args = parser.parse_args()
    plan = build_plan(args.run_id or str(uuid.uuid4()))
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
