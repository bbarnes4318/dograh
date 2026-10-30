"""The platform block list: numbers no call may be placed to or accepted from.

The numbers live in ``blocked_numbers.txt`` next to this module, one ten-digit
US number per line. Hopwhistle enforces the same list in FreeSWITCH and in its
API (``apps/api/src/lib/blocked-numbers.ts``); this copy covers the calls
Dograh places or answers itself:

- every outbound call, before a workflow run is started or a provider is asked
  to dial (campaigns, the public agent API, test calls);
- every inbound call, over ARI or a provider webhook, before it is answered.
"""

import re
from functools import lru_cache
from pathlib import Path

_LIST_FILE = Path(__file__).with_name("blocked_numbers.txt")
_NON_DIGIT_RE = re.compile(r"\D")
_TEN_DIGITS_RE = re.compile(r"^\d{10}$")


@lru_cache(maxsize=1)
def blocked_numbers() -> frozenset[str]:
    """The block list, as ten-digit strings."""
    lines = _LIST_FILE.read_text().splitlines()
    return frozenset(
        line.strip() for line in lines if _TEN_DIGITS_RE.match(line.strip())
    )


def ten_digits(raw: str | None) -> str | None:
    stripped = (raw or "").strip()
    if stripped.lower().startswith(("sip:", "sips:")) or "@" in stripped:
        return None
    digits = _NON_DIGIT_RE.sub("", stripped)
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    if len(digits) == 10:
        return digits
    return None


def is_blocked_number(raw: str | None) -> bool:
    """Whether a number, in any common US format, is on the block list."""
    digits = ten_digits(raw)
    return digits is not None and digits in blocked_numbers()
