"""Isolated R2 backup/restore integration probe; dry-run by default.

Dry-run: python r2_backup_integration.py
Live run: python r2_backup_integration.py --execute
A live run requires interactive confirmation and writes ONLY to the dedicated
test bucket. It NEVER deletes anything, touches the production bucket, or uses
production credentials. Do not run --execute until explicitly authorised.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import uuid

from r2_isolated_test import build_plan
from r2_test_bucket_check import TEST_BUCKET, TEST_ROOT
from r2_test_credentials import TEST_SERVICE

ORIGINAL = b"Street Face disposable R2 test: ORIGINAL v1\n"
REPLACEMENT = b"Street Face disposable R2 test: REPLACEMENT v2\n"
CONFIRMATION = "WRITE TO ISOLATED TEST BUCKET"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _assert_key(key: str, prefix: str) -> None:
    if not key.startswith(TEST_ROOT) or not key.startswith(prefix):
        raise ValueError("Object key outside the current test run")
    if key.startswith("street-face/hebden-bridge/v2/"):
        raise ValueError("Production artwork key forbidden")


def _read(client, key: str, prefix: str) -> bytes:
    _assert_key(key, prefix)
    return client.get_object(Bucket=TEST_BUCKET, Key=key)["Body"].read()


def _verify(client, key: str, prefix: str, expected: bytes) -> None:
    actual = _read(client, key, prefix)
    if _sha(actual) != _sha(expected):
        raise RuntimeError(f"SHA-256 mismatch for isolated test key {key}")


def execute_probe(client, run_id: str, *, authorized: bool = False) -> dict:
    """Write ONLY isolated disposable objects when caller explicitly authorises.

    No automatic cleanup: retained objects permit forensic review if interrupted.
    """
    if not authorized:
        raise PermissionError("Explicit live-test authorisation required")
    plan = build_plan(run_id)
    prefix = plan["prefix"]
    keys = plan["objects"]
    # Build-plan validates UUID4, test prefix and key uniqueness.
    if TEST_BUCKET == "mug-print-assets":
        raise RuntimeError("Test bucket must differ from production")
    for key in keys.values():
        _assert_key(key, prefix)

    client.head_bucket(Bucket=TEST_BUCKET)
    # Fail closed if this run-prefix already contains anything; never overwrite
    # arbitrary existing objects. Subsequent overwrite is solely our own object.
    listing = client.list_objects_v2(Bucket=TEST_BUCKET, Prefix=prefix, MaxKeys=1)
    if listing.get("Contents") or listing.get("IsTruncated"):
        raise RuntimeError("Test run prefix is not empty; refusing writes")

    original_key, backup_key = keys["original"], keys["backup"]
    stages = []
    client.put_object(Bucket=TEST_BUCKET, Key=original_key, Body=ORIGINAL)
    _verify(client, original_key, prefix, ORIGINAL)
    stages.append("disposable original uploaded and verified")

    client.copy_object(
        Bucket=TEST_BUCKET, Key=backup_key,
        CopySource={"Bucket": TEST_BUCKET, "Key": original_key},
    )
    _verify(client, backup_key, prefix, ORIGINAL)
    stages.append("backup copied and SHA-256 verified")

    client.put_object(Bucket=TEST_BUCKET, Key=original_key, Body=REPLACEMENT)
    _verify(client, original_key, prefix, REPLACEMENT)
    _verify(client, backup_key, prefix, ORIGINAL)
    stages.append("disposable original replaced; backup remained intact")

    # Restore the ORIGINAL, using the actual R2 server-side backup copy.
    client.copy_object(
        Bucket=TEST_BUCKET, Key=original_key,
        CopySource={"Bucket": TEST_BUCKET, "Key": backup_key},
    )
    _verify(client, original_key, prefix, ORIGINAL)
    stages.append("original bytes restored from backup and SHA-256 verified")

    return {"bucket": TEST_BUCKET, "run_id": run_id, "prefix": prefix,
            "stages": stages, "restored_sha256": _sha(ORIGINAL),
            "backup_sha256": _sha(ORIGINAL), "objects_retained": True,
            "deletions": 0,
            "limitation": "Does not test an actual network-interrupted upload or the production publisher."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true",
                        help="Allow test-only R2 writes after interactive confirmation")
    args = parser.parse_args()
    run_id = str(uuid.uuid4())
    plan = build_plan(run_id)
    if not args.execute:
        print(json.dumps({
            "mode": "DRY_RUN_ONLY", "bucket": TEST_BUCKET,
            "run_id": run_id, "prefix": plan["prefix"],
            "cloud_calls": 0, "writes": 0, "deletes": 0,
            "next_step": "Run offline tests and obtain explicit authorisation before --execute.",
        }, indent=2))
        return 0

    print(f"TEST BUCKET: {TEST_BUCKET}")
    print(f"NEW DISPOSABLE RUN PREFIX: {plan['prefix']}")
    print("This will create, overwrite and restore ONLY disposable test objects.")
    print("Objects are RETAINED; no automatic deletion occurs.")
    if input(f"Type '{CONFIRMATION}' exactly to proceed: ").strip() != CONFIRMATION:
        print("Cancelled. No cloud calls made.")
        return 1

    import boto3
    import keyring

    account = keyring.get_password(TEST_SERVICE, "account")
    access = keyring.get_password(TEST_SERVICE, "access")
    secret = keyring.get_password(TEST_SERVICE, "secret")
    if not all((account, access, secret)):
        print("Missing isolated R2 test credentials. No cloud calls made.")
        return 1
    client = boto3.client(
        "s3", region_name="auto",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=access, aws_secret_access_key=secret,
    )
    try:
        result = execute_probe(client, run_id, authorized=True)
    except Exception as exc:
        # Do not leak raw Cloudflare SDK errors containing endpoint or headers.
        print(f"Test stopped: {type(exc).__name__}. Test objects may remain; "
              f"inspect ONLY prefix {plan['prefix']}. No automatic cleanup.")
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
