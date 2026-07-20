"""The login gate for Component A's web UI.

⚠️ Pure functions plus one FastAPI dependency. No database, no network — the
credential is a hash carried in the environment, so authentication has nothing
to look up and cannot be broken by an outage.

## What this is, and is not

One operator credential, a signed session cookie, nothing else. There is no
registration, no password reset, no second user, no role. That is the whole
requirement: keep strangers off a public endpoint that queues work and will,
in Part 2, spend LLM credits per submission.

⛔ It is NOT a general auth system, and should not grow into one here. If this
ever needs real accounts, the answer is an identity provider, not more code in
this file.

## 🔒 Empty is locked, not open

An unset `ADMIN_PASSWORD_HASH` rejects every password. A stack nobody has
configured is therefore inaccessible rather than wide open — the failure mode
of a misconfiguration must be a locked door.

Hash format and cost parameters live in `infra/scripts/hash-password.py`, which
generates them. The parameters are read from the STORED hash rather than from
today's constants, so raising the cost later does not invalidate the credential
already in the environment.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import Request
from fastapi.responses import RedirectResponse

log = logging.getLogger("gliq-intake")

#: Session key holding the authenticated username.
SESSION_USER = "user"

#: Where an unauthenticated request is sent.
LOGIN_PATH = "/login"

#: Paths reachable without a session.
#:
#: ⚠️ `/healthz` is deliberately open: it is the deployment check, is called by
#: the operator and by nothing else, and exposes only configuration facts that
#: are already public knowledge (provider name, topic name, corpus size). It
#: must never grow a field that is sensitive.
PUBLIC_PATHS = frozenset({"/healthz", LOGIN_PATH, "/favicon.ico"})


def _maxmem(n: int, r: int) -> int:
    """Memory ceiling for scrypt, derived from the cost parameters.

    ⚠️ Required. scrypt needs ~`128 * n * r` bytes and OpenSSL's default ceiling
    is exactly 32MB, which the parameters in hash-password.py hit precisely —
    without this, every verification fails with "memory limit exceeded" and
    presents as "the correct password is rejected".
    """
    return 128 * n * r * 2


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time check of a password against a stored scrypt hash.

    ⚠️ Every failure path returns False rather than raising. A malformed hash,
    an empty hash, an empty password — all are a failed login. Raising here
    would turn a misconfiguration into a 500 that leaks the difference between
    "wrong password" and "no password configured".

    ⚠️ Mirrors `infra/scripts/hash-password.py::verify_password` deliberately:
    the generator is an ops script that never ships to the VM, and the intake
    component must not depend on `infra/`. `tests/test_intake_web.py` pins the
    two against each other so they cannot drift.
    """
    if not password or not stored_hash:
        return False
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored_hash.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(hash_hex)
        derived = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=_maxmem(int(n), int(r)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, expected)


def current_user(request: Request) -> str | None:
    """The logged-in username, or None."""
    return request.session.get(SESSION_USER)


def log_in(request: Request, username: str) -> None:
    request.session[SESSION_USER] = username


def log_out(request: Request) -> None:
    request.session.clear()


class NotLoggedIn(Exception):
    """Raised by `require_session`; the app turns it into a redirect."""


def require_session(request: Request) -> str:
    """FastAPI dependency guarding every non-public route.

    Returns the username so a handler can display it.
    """
    user = current_user(request)
    if not user:
        raise NotLoggedIn()
    return user


def redirect_to_login(request: Request) -> RedirectResponse:
    """Send an unauthenticated browser to the login form.

    ⚠️ 303, not 302: the guard fires on POSTs too, and 302 lets a client replay
    the original method against `/login`. 303 forces a GET, which is what a
    login form is.

    The path that was wanted is carried in `?next=` so the user lands where they
    were headed. ⚠️ Only the path and query are kept — accepting a full URL here
    is an open-redirect, and `next` comes from the request.
    """
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(url=f"{LOGIN_PATH}?next={target}", status_code=303)


def safe_next(raw: str | None, fallback: str = "/") -> str:
    """Sanitise a `?next=` value before redirecting to it.

    ⚠️ Must start with a single `/`. Rejecting `//host` matters as much as
    rejecting `https://host`: a protocol-relative URL is a fully functional
    off-site redirect, and it is the case people forget.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return fallback
    return raw
