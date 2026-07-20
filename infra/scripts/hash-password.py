#!/usr/bin/env python3
"""Hash the web UI's admin password for `pulumi config set`.

    python3 infra/scripts/hash-password.py
    python3 infra/scripts/hash-password.py --check '<hash>'

Component A's login gate stores a HASH, never the password. This prints the
hash to paste into:

    cd infra && pulumi config set --secret adminPasswordHash '<hash>'

⚠️ The plaintext password is never written to disk, never echoed, and never
stored in Pulumi state — only the derived hash travels. Losing it means
generating a new hash, which is a two-command recovery rather than a problem.

## Why scrypt from the standard library

`hashlib.scrypt` is memory-hard and ships with CPython, so the login path adds
no dependency to the intake VM. bcrypt/argon2 would be equally defensible and
both would mean a wheel on the box for one comparison per login.

The stored format is self-describing, so the verifier reads its parameters from
the hash rather than assuming today's constants:

    scrypt$<n>$<r>$<p>$<salt-hex>$<hash-hex>

That matters because the cost parameters below should rise over time, and old
hashes must keep verifying after they do.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import os
import sys

#: OWASP-recommended scrypt parameters. n is the CPU/memory cost and must be a
#: power of two; 2**15 with r=8 needs ~32MB per hash, which is trivial for one
#: login and expensive for an attacker with a stolen hash.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_LEN = 32


def _maxmem(n: int, r: int) -> int:
    """Memory ceiling to hand OpenSSL, derived from the cost parameters.

    ⚠️ Not optional. scrypt needs roughly `128 * n * r` bytes — 32MB at the
    parameters above — and OpenSSL's default ceiling is exactly 32MB, so the
    call fails with "memory limit exceeded" without this. Computing it from the
    parameters rather than hardcoding means raising SCRYPT_N later does not
    silently reintroduce the same error.
    """
    return 128 * n * r * 2

MIN_LENGTH = 12


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else os.urandom(SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_LEN,
        maxmem=_maxmem(SCRYPT_N, SCRYPT_R),
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check against a stored hash.

    ⚠️ Parameters come from the STORED hash, not from the constants above, so a
    later increase in cost does not invalidate existing credentials.
    """
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(hash_hex)),
            maxmem=_maxmem(int(n), int(r)),
        )
    except (ValueError, TypeError):
        # A malformed or empty hash is a failed login, never a crash — an
        # unset ADMIN_PASSWORD_HASH must lock the door, not open it.
        return False
    return hmac.compare_digest(derived, bytes.fromhex(hash_hex))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", metavar="HASH", help="verify a password against a hash")
    args = parser.parse_args()

    password = getpass.getpass("Password: ")

    if args.check:
        ok = verify_password(password, args.check)
        print("✅ password matches the hash" if ok else "❌ password does NOT match")
        return 0 if ok else 1

    if len(password) < MIN_LENGTH:
        print(f"❌ too short — use at least {MIN_LENGTH} characters", file=sys.stderr)
        return 1
    if password != getpass.getpass("Confirm: "):
        print("❌ passwords do not match", file=sys.stderr)
        return 1

    print()
    print("✅ hash generated. Set it with:")
    print()
    print(f"  cd infra && pulumi config set --secret adminPasswordHash '{hash_password(password)}'")
    print()
    print("📝 Then `./infra/scripts/deploy.sh intake` ships it to /etc/gliq/gliq.env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
