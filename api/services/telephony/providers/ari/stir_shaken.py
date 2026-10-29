"""STIR/SHAKEN signing of outbound FracTEL calls with PVN's certificate.

Adds an ``Identity`` header (and the matching ``Date``) to calls dialled on
the ``fractel`` trunk. The files live in ``ARI_STIR_SHAKEN_DIR`` (default
``/run/pvn-stir``, a read-only bind mount on hopwhistle-prod-ash):

- ``ENABLED``: signing runs only while this file exists.
- ``pvn-private-key.pem``: P-256 private key.
- ``pvn-certificate.pem``: its certificate.
- ``pvn-shaken.json``: ``{"x5u": <certificate URL>, "origid": <uuid>}``.
- ``pvn-dids.txt``: one ``1XXXXXXXXXX`` number per line. These callers get
  attestation A, everyone else B.

Any problem means no header: the call goes out exactly as it would unsigned
and is never blocked. Neither the key nor the header is ever logged.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from email.utils import formatdate
from typing import Any

from loguru import logger

_DEFAULT_DIR = "/run/pvn-stir"
_FRACTEL_ENDPOINT = re.compile(r"PJSIP/(?:[0-9*]+#)?\+?1?([2-9]\d{9})@fractel")
_NANP = re.compile(r"\+?1?([2-9]\d{9})")

# Loaded material, reloaded when pvn-dids.txt changes.
_cache: dict[str, Any] = {}


def _directory() -> str:
    return os.environ.get("ARI_STIR_SHAKEN_DIR", "").strip() or _DEFAULT_DIR


def _cert_time(cert, name: str) -> float:
    from datetime import timezone

    value = getattr(cert, name + "_utc", None)
    if value is None:
        value = getattr(cert, name).replace(tzinfo=timezone.utc)
    return value.timestamp()


def _load(directory: str) -> dict[str, Any]:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    with open(os.path.join(directory, "pvn-private-key.pem"), "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    with open(os.path.join(directory, "pvn-certificate.pem"), "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise ValueError("key is not P-256")
    if key.public_key().public_numbers() != cert.public_key().public_numbers():
        raise ValueError("key does not match certificate")
    with open(os.path.join(directory, "pvn-shaken.json")) as f:
        conf = json.load(f)
    with open(os.path.join(directory, "pvn-dids.txt")) as f:
        dids = {line.strip() for line in f if line.strip()}
    return {
        "key": key,
        "not_before": _cert_time(cert, "not_valid_before"),
        "not_after": _cert_time(cert, "not_valid_after"),
        "x5u": conf["x5u"],
        "origid": conf["origid"],
        "dids": dids,
    }


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _encode(obj: dict) -> str:
    return _b64(json.dumps(obj, separators=(",", ":"), sort_keys=True).encode())


def identity_variables(sip_endpoint: str, caller: str | None) -> dict[str, str]:
    """ARI channel variables adding the signed ``Identity`` and its ``Date``
    header to a FracTEL-bound call, or ``{}`` when it is not signed."""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import (
            decode_dss_signature,
        )

        directory = _directory()
        if not os.path.exists(os.path.join(directory, "ENABLED")):
            return {}
        dest_match = _FRACTEL_ENDPOINT.fullmatch(sip_endpoint or "")
        orig_match = _NANP.fullmatch(re.sub(r"[^\d+]", "", caller or ""))
        if not dest_match or not orig_match:
            return {}

        mtime = os.path.getmtime(os.path.join(directory, "pvn-dids.txt"))
        if _cache.get("mtime") != mtime or _cache.get("directory") != directory:
            _cache.clear()
            _cache.update(_load(directory))
            _cache["mtime"] = mtime
            _cache["directory"] = directory

        now = int(time.time())
        if not (_cache["not_before"] <= now < _cache["not_after"]):
            logger.error("[PVN-SHAKEN] certificate not valid now - call sent unsigned")
            return {}

        dest, orig = "1" + dest_match.group(1), "1" + orig_match.group(1)
        attest = "A" if orig in _cache["dids"] else "B"
        header = _encode(
            {"alg": "ES256", "ppt": "shaken", "typ": "passport", "x5u": _cache["x5u"]}
        )
        payload = _encode(
            {
                "attest": attest,
                "dest": {"tn": [dest]},
                "iat": now,
                "orig": {"tn": orig},
                "origid": _cache["origid"],
            }
        )
        r, s = decode_dss_signature(
            _cache["key"].sign(
                f"{header}.{payload}".encode(), ec.ECDSA(hashes.SHA256())
            )
        )
        signature = _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        logger.info(f"[PVN-SHAKEN] signed attest={attest}")
        return {
            "PJSIP_HEADER(add,Identity)": (
                f"{header}.{payload}.{signature};info=<{_cache['x5u']}>;"
                "alg=ES256;ppt=shaken"
            ),
            "PJSIP_HEADER(add,Date)": formatdate(now, usegmt=True),
        }
    except Exception as e:
        logger.error(
            f"[PVN-SHAKEN] not signed ({type(e).__name__}) - call sent unsigned"
        )
        return {}
