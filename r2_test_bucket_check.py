"""Read-only connectivity and isolation check for the dedicated R2 test bucket.

Run: python r2_test_bucket_check.py

Uses existing Windows Credential Manager R2 credentials, but ignores the saved
production bucket name. Reads only the fixed test bucket. No object writes.
"""
from __future__ import annotations

import sys

TEST_BUCKET = "street-face-integration-tests"
PRODUCTION_BUCKET = "mug-print-assets"
TEST_ROOT = "street-face-integration-tests/v1/"
SERVICE = "street-face-artwork-manager"


def check_test_bucket(client, bucket: str = TEST_BUCKET) -> dict:
    """Read-only bucket existence and test-prefix check. Refuse other buckets."""
    if bucket != TEST_BUCKET or bucket == PRODUCTION_BUCKET:
        raise ValueError("Only the dedicated integration-test bucket is allowed")
    client.head_bucket(Bucket=bucket)
    objects = client.list_objects_v2(Bucket=bucket, Prefix=TEST_ROOT, MaxKeys=5)
    keys = [obj["Key"] for obj in objects.get("Contents", [])]
    return {
        "bucket": bucket,
        "bucket_accessible": True,
        "test_prefix": TEST_ROOT,
        "test_prefix_has_objects": bool(keys),
        "sample_object_count": len(keys),
        "read_only": True,
        "write_calls": 0,
        "note": "A successful result establishes API access, not permission to upload or delete.",
    }


def main() -> int:
    import boto3
    import keyring
    account = keyring.get_password(SERVICE, "account")
    access = keyring.get_password(SERVICE, "access")
    secret = keyring.get_password(SERVICE, "secret")
    if not all((account, access, secret)):
        print("Missing saved R2 credentials. Load or save them in Artwork Manager.", file=sys.stderr)
        return 1
    client = boto3.client(
        "s3", region_name="auto",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=access, aws_secret_access_key=secret,
    )
    try:
        result = check_test_bucket(client)
    except Exception as exc:
        # Don't print raw SDK errors: they can include sensitive request details.
        print(f"Read-only test bucket check failed: {type(exc).__name__}. "
              "Existing credentials may not have access to the new bucket.", file=sys.stderr)
        return 1
    print(f"TEST BUCKET: {result['bucket']}")
    print("ACCESSIBLE: YES")
    print(f"TEST PREFIX: {result['test_prefix']}")
    print(f"PREFIX CONTAINS OBJECTS: {'YES' if result['test_prefix_has_objects'] else 'NO'}")
    print("CLOUD WRITES: NONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
