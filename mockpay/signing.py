"""Webhook signing scheme shared by MockPay (signer) and StayPay (verifier).

Header:  MockPay-Signature: t=<unix_ts>,v1=<hex hmac_sha256(secret, f"{t}.{raw_body}")>
Same shape as most real processors: the timestamp is signed to stop replay attacks.
"""

import hashlib
import hmac
import time

SIGNATURE_HEADER = "MockPay-Signature"


def sign(secret: str, body: bytes, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


class SignatureError(Exception):
    pass


def verify(secret: str, body: bytes, header: str | None, tolerance_s: int = 300) -> None:
    if not header:
        raise SignatureError("missing signature header")
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
        given = parts["v1"]
    except (ValueError, KeyError) as e:
        raise SignatureError("malformed signature header") from e
    if abs(time.time() - ts) > tolerance_s:
        raise SignatureError("timestamp outside tolerance")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, given):
        raise SignatureError("signature mismatch")
