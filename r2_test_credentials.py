"""Save dedicated R2 test credentials without modifying production credentials.

Run: python r2_test_credentials.py
The access key and secret are entered invisibly and stored in Windows Credential
Manager under a separate service. No cloud connection or object writes occur.
"""
from __future__ import annotations

import getpass
import sys

TEST_SERVICE = "street-face-artwork-manager-r2-tests"
PRODUCTION_SERVICE = "street-face-artwork-manager"
FIELDS = ("account", "access", "secret")


def store_test_credentials(store, account: str, access: str, secret: str) -> None:
    """Only write the isolated test-service entries."""
    if TEST_SERVICE == PRODUCTION_SERVICE:
        raise RuntimeError("Test credentials must use separate storage")
    values = {"account": account.strip(), "access": access.strip(), "secret": secret.strip()}
    if not all(values.values()) or any("\n" in value or "\r" in value for value in values.values()):
        raise ValueError("All three credential fields must be nonempty single-line values")
    if not values["account"].isalnum():
        raise ValueError("Cloudflare account ID must contain only letters and numbers")
    for field in FIELDS:
        store.set_password(TEST_SERVICE, field, values[field])


def main() -> int:
    import keyring

    account = keyring.get_password(PRODUCTION_SERVICE, "account")
    if not account:
        print("No saved Cloudflare account ID found in Artwork Manager.", file=sys.stderr)
        return 1
    print("Storing test credentials separately; production credentials will not be changed.")
    print("Enter the new test token's Access Key ID and Secret Access Key.")
    access = getpass.getpass("Test Access Key ID (hidden): ")
    secret = getpass.getpass("Test Secret Access Key (hidden): ")
    try:
        store_test_credentials(keyring, account, access, secret)
    except (ValueError, RuntimeError) as exc:
        print(f"Test credentials NOT stored: {exc}", file=sys.stderr)
        return 1
    print("Saved dedicated R2 test credentials in Windows Credential Manager.")
    print("Production credential entries unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
