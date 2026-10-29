"""How the ARI provider turns a number into an Asterisk dial string.

With every setting below unset, behavior is unchanged from before this
module existed: only ``SIP/`` and ``PJSIP/`` destinations pass through, and
everything else becomes ``PJSIP/<number>[@ARI_PJSIP_DEFAULT_TRUNK]``.

Each setting is off unless set. Each one carries a behavior that production
previously got only from a bind-mounted patch of ``provider.py``. The mapping
from those patches to these names is recorded in
``deploy/hopwhistle/cutover/mount_manifest.json``.

``ARI_PJSIP_DEFAULT_TRUNK``
    Trunk for outbound calls, and for transfers unless overridden.
``ARI_PJSIP_TRANSFER_TRUNK``
    Trunk for transfer legs. Defaults to ``ARI_PJSIP_DEFAULT_TRUNK``.
``ARI_DIAL_ANY_TECHNOLOGY``
    ``true``: any ``Tech/...`` destination (``Local/``, ``IAX2/``, ...) is
    dialled as written, not only ``SIP/`` and ``PJSIP/``.
``ARI_DIAL_PREFIX``
    Digits put in front of outbound NANP numbers as ``<prefix>1XXXXXXXXXX``.
    Never applied to transfers.
``ARI_DIAL_FORMAT``
    ``e164`` (default, unchanged), ``nanp11`` (``1XXXXXXXXXX``) or ``nanp10``
    (``XXXXXXXXXX``) for outbound NANP numbers. A prefix, when set, wins.
    Never applied to transfers.
``ARI_CALLER_TRUNKS``
    ``trunk[/format]:number,number;trunk[/format]:number``. An outbound call
    whose caller ID is listed goes out on that trunk, with the destination and
    caller ID written in that format. Matched on the last ten digits. A
    malformed entry is ignored, never fatal. Never applied to transfers.
``ARI_FRACTEL_TECH_PREFIX``
    FracTEL picks the route/account by a tech prefix (e.g. ``6125#``). When
    set, every NANP number dialled on the ``fractel`` trunk, outbound calls
    and transfers alike, becomes ``<prefix>1XXXXXXXXXX``. Wins over
    ``ARI_DIAL_PREFIX``/``ARI_DIAL_FORMAT`` on that trunk. Numbers routed by
    ``ARI_CALLER_TRUNKS`` are left alone.

An ARI configuration can also send outbound calls through the Mash SBC
(``outbound_carrier="mash"``): the destination then goes out as
``PJSIP/1XXXXXXXXXX@mash``, unless ``ARI_CALLER_TRUNKS`` routes the caller ID.
"""

from __future__ import annotations

import os
import re

_TECH_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9]*/")
_TRUNK_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_NANP_FORMATS = ("nanp11", "nanp10")


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _truthy(name: str) -> bool:
    return _env(name).lower() in ("1", "true", "yes", "on")


def _has_technology(destination: str) -> bool:
    """True when ``destination`` is already a dial string, not a number."""
    if destination.startswith(("SIP/", "PJSIP/")):
        return True
    return _truthy("ARI_DIAL_ANY_TECHNOLOGY") and bool(
        _TECH_PREFIX.match(destination.strip())
    )


def _with_trunk(number: str, trunk: str) -> str:
    # A number that already names an endpoint ("1001@office") is left alone.
    if trunk and "@" not in number:
        return f"PJSIP/{number}@{trunk}"
    return f"PJSIP/{number}"


def _prefixed(number: str, prefix: str) -> str:
    digits = re.sub(r"\D", "", number)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return number  # not a NANP number: leave it alone
    return f"{prefix}1{digits}"


def _formatted(number: str, fmt: str) -> str:
    if fmt not in _NANP_FORMATS:
        return number
    if not re.fullmatch(r"\+?1?\d{10}", number.strip()):
        return number  # not a bare NANP number: leave it alone
    digits = re.sub(r"\D", "", number)[-10:]
    return digits if fmt == "nanp10" else f"1{digits}"


def _outbound_number(number: str) -> str:
    prefix = re.sub(r"\D", "", _env("ARI_DIAL_PREFIX"))
    if prefix:
        return _prefixed(number, prefix)
    return _formatted(number, _env("ARI_DIAL_FORMAT").lower() or "e164")


def _fractel_prefixed(number: str, trunk: str) -> str | None:
    """``<prefix>1XXXXXXXXXX`` for a NANP number on the fractel trunk while
    ``ARI_FRACTEL_TECH_PREFIX`` is set, else None."""
    prefix = _env("ARI_FRACTEL_TECH_PREFIX")
    if trunk != "fractel" or not prefix:
        return None
    match = re.fullmatch(r"\+?1?([2-9]\d{9})", re.sub(r"[^\d+]", "", number))
    return f"{prefix}1{match.group(1)}" if match else None


def _caller_routes() -> dict[str, tuple[str, str]]:
    routes: dict[str, tuple[str, str]] = {}
    for part in _env("ARI_CALLER_TRUNKS").split(";"):
        head, sep, numbers = part.partition(":")
        trunk, _, fmt = head.strip().partition("/")
        if not sep or not _TRUNK_NAME.fullmatch(trunk):
            continue
        fmt = (fmt or "e164").strip().lower()
        for number in numbers.split(","):
            digits = re.sub(r"\D", "", number)[-10:]
            if len(digits) == 10:
                routes[digits] = (trunk, fmt)
    return routes


def _caller_route(caller: str | None) -> tuple[str, str] | None:
    if not caller:
        return None
    return _caller_routes().get(re.sub(r"\D", "", caller)[-10:])


def _route_format(number: str, fmt: str) -> str:
    digits = re.sub(r"\D", "", number or "")
    if fmt not in _NANP_FORMATS or not re.fullmatch(r"1?\d{10}", digits):
        return number
    return digits[-10:] if fmt == "nanp10" else "1" + digits[-10:]


def outbound_endpoint(
    to_number: str, caller: str | None = None, carrier: str = "fractel"
) -> str:
    """Dial string for an outbound AI call. ``carrier`` is the ARI
    configuration's ``outbound_carrier``."""
    if _has_technology(to_number):
        return to_number
    route = _caller_route(caller)
    if route:
        trunk, fmt = route
        return f"PJSIP/{_route_format(to_number, fmt)}@{trunk}"
    if carrier == "mash":
        return f"PJSIP/{_route_format(to_number, 'nanp11')}@mash"
    trunk = _env("ARI_PJSIP_DEFAULT_TRUNK")
    prefixed = _fractel_prefixed(to_number, trunk)
    if prefixed:
        return f"PJSIP/{prefixed}@{trunk}"
    return _with_trunk(_outbound_number(to_number), trunk)


def outbound_caller_id(caller: str | None) -> str | None:
    """Caller ID as the chosen trunk expects it written."""
    route = _caller_route(caller)
    return _route_format(caller, route[1]) if route and caller else caller


def transfer_endpoint(destination: str) -> str:
    """Dial string for a transfer leg. Never prefixed or reformatted, except
    by ``ARI_FRACTEL_TECH_PREFIX`` on the fractel trunk."""
    if _has_technology(destination):
        return destination
    trunk = _env("ARI_PJSIP_TRANSFER_TRUNK") or _env("ARI_PJSIP_DEFAULT_TRUNK")
    prefixed = _fractel_prefixed(destination, trunk)
    if prefixed:
        return f"PJSIP/{prefixed}@{trunk}"
    return _with_trunk(destination, trunk)
