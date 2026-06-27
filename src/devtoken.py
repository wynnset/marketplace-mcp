#!/usr/bin/env python3
"""
devtoken.py — mint an HS256 device token for the relay's /agent leg.

Used by `./finder provision <email>` (Step 5) to bake a friend's device token into
their installer. The relay Worker verifies it (relay/src/index.ts → verifyDeviceJWT):
HS256 over RELAY_JWT_SECRET, pinned alg, `aud == "relay-agent"`, and a non-empty
`sub` (the friend's email, lowercased — the routing key that must match the email
they sign into Google with). Device tokens are long-lived and carry no `exp`;
revocation is the Worker's KV denylist (by `sub`/`jti`), not expiry.

The shared secret is what lets `finder` mint offline and the Worker verify without
any pre-registration. Keep RELAY_JWT_SECRET secret and identical on both sides.

Usage:
    RELAY_JWT_SECRET=... python src/devtoken.py <email>      # prints the token
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time

AGENT_AUD = "relay-agent"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def mint_device_token(
    email: str,
    secret: str,
    *,
    aud: str = AGENT_AUD,
    jti: str | None = None,
    exp: int | None = None,
) -> str:
    """Return a signed HS256 device token for `email`. Raises on bad input."""
    sub = email.strip().lower()
    if not sub:
        raise ValueError("email (sub) is required")
    if not secret:
        raise ValueError("RELAY_JWT_SECRET is required to sign the token")
    header = {"alg": "HS256", "typ": "JWT"}
    payload: dict = {"sub": sub, "aud": aud}
    if jti is not None:
        payload["jti"] = jti
    if exp is not None:
        payload["exp"] = exp
    signing_input = (
        f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}."
        f"{_b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    )
    sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(sig)}"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print("usage: RELAY_JWT_SECRET=... python src/devtoken.py <email>", file=sys.stderr)
        return 2
    secret = os.environ.get("RELAY_JWT_SECRET", "")
    if not secret:
        print("error: RELAY_JWT_SECRET is not set", file=sys.stderr)
        return 1
    email = argv[1].strip().lower()
    # A stable-ish jti for traceability + targeted revocation: email + mint time.
    jti = f"{email}-{int(time.time())}"
    try:
        print(mint_device_token(email, secret, jti=jti))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
