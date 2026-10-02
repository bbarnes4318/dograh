"""Canonical NANP area-code (NPA) -> US state mapping for Dograh caller-ID selection.

GENERATED FROM apps/api/src/lib/geo.ts (AREA_CODE_TO_STATE + NON_GEOGRAPHIC_CODES).
Keep the two in sync: if you edit geo.ts, regenerate this dict (see deploy/dograh/README.md).

Deployed to the Dograh api container as api/services/campaign/areacode_state.py via
docker-compose.override.yaml (see /opt/dograh/docker-compose.override.yaml on the box).

Only US states + DC are geographic here. Toll-free / premium / personal codes are
non-geographic. US territories (PR/VI/GU/AS/MP), Canada, and unknown NPAs resolve to
no state and are reported with a distinct reason so callers can fail closed.
"""

from __future__ import annotations

AREA_CODE_TO_STATE: dict[str, str] = {
    "205": "AL",
    "251": "AL",
    "256": "AL",
    "334": "AL",
    "938": "AL",
    "907": "AK",
    "480": "AZ",
    "520": "AZ",
    "602": "AZ",
    "623": "AZ",
    "928": "AZ",
    "479": "AR",
    "501": "AR",
    "870": "AR",
    "209": "CA",
    "213": "CA",
    "279": "CA",
    "310": "CA",
    "323": "CA",
    "341": "CA",
    "408": "CA",
    "415": "CA",
    "424": "CA",
    "442": "CA",
    "510": "CA",
    "530": "CA",
    "559": "CA",
    "562": "CA",
    "619": "CA",
    "626": "CA",
    "628": "CA",
    "650": "CA",
    "657": "CA",
    "661": "CA",
    "669": "CA",
    "707": "CA",
    "714": "CA",
    "747": "CA",
    "760": "CA",
    "805": "CA",
    "818": "CA",
    "820": "CA",
    "831": "CA",
    "858": "CA",
    "909": "CA",
    "916": "CA",
    "925": "CA",
    "949": "CA",
    "951": "CA",
    "303": "CO",
    "719": "CO",
    "720": "CO",
    "970": "CO",
    "203": "CT",
    "475": "CT",
    "860": "CT",
    "959": "CT",
    "302": "DE",
    "239": "FL",
    "305": "FL",
    "321": "FL",
    "352": "FL",
    "386": "FL",
    "407": "FL",
    "561": "FL",
    "727": "FL",
    "754": "FL",
    "772": "FL",
    "786": "FL",
    "813": "FL",
    "850": "FL",
    "863": "FL",
    "904": "FL",
    "941": "FL",
    "954": "FL",
    "229": "GA",
    "404": "GA",
    "470": "GA",
    "478": "GA",
    "678": "GA",
    "706": "GA",
    "762": "GA",
    "770": "GA",
    "912": "GA",
    "943": "GA",
    "808": "HI",
    "208": "ID",
    "986": "ID",
    "217": "IL",
    "224": "IL",
    "309": "IL",
    "312": "IL",
    "331": "IL",
    "618": "IL",
    "630": "IL",
    "708": "IL",
    "773": "IL",
    "779": "IL",
    "815": "IL",
    "847": "IL",
    "872": "IL",
    "219": "IN",
    "260": "IN",
    "317": "IN",
    "463": "IN",
    "574": "IN",
    "765": "IN",
    "812": "IN",
    "930": "IN",
    "319": "IA",
    "515": "IA",
    "563": "IA",
    "641": "IA",
    "712": "IA",
    "316": "KS",
    "620": "KS",
    "785": "KS",
    "913": "KS",
    "270": "KY",
    "364": "KY",
    "502": "KY",
    "606": "KY",
    "859": "KY",
    "225": "LA",
    "318": "LA",
    "337": "LA",
    "504": "LA",
    "985": "LA",
    "207": "ME",
    "240": "MD",
    "301": "MD",
    "410": "MD",
    "443": "MD",
    "667": "MD",
    "339": "MA",
    "351": "MA",
    "413": "MA",
    "508": "MA",
    "617": "MA",
    "774": "MA",
    "781": "MA",
    "857": "MA",
    "978": "MA",
    "231": "MI",
    "248": "MI",
    "269": "MI",
    "313": "MI",
    "517": "MI",
    "586": "MI",
    "616": "MI",
    "734": "MI",
    "810": "MI",
    "906": "MI",
    "947": "MI",
    "989": "MI",
    "218": "MN",
    "320": "MN",
    "507": "MN",
    "612": "MN",
    "651": "MN",
    "763": "MN",
    "952": "MN",
    "228": "MS",
    "601": "MS",
    "662": "MS",
    "769": "MS",
    "314": "MO",
    "417": "MO",
    "573": "MO",
    "636": "MO",
    "660": "MO",
    "816": "MO",
    "406": "MT",
    "308": "NE",
    "402": "NE",
    "531": "NE",
    "702": "NV",
    "725": "NV",
    "775": "NV",
    "603": "NH",
    "201": "NJ",
    "551": "NJ",
    "609": "NJ",
    "732": "NJ",
    "848": "NJ",
    "856": "NJ",
    "862": "NJ",
    "908": "NJ",
    "973": "NJ",
    "505": "NM",
    "575": "NM",
    "212": "NY",
    "315": "NY",
    "332": "NY",
    "347": "NY",
    "516": "NY",
    "518": "NY",
    "585": "NY",
    "607": "NY",
    "631": "NY",
    "646": "NY",
    "680": "NY",
    "716": "NY",
    "718": "NY",
    "838": "NY",
    "845": "NY",
    "914": "NY",
    "917": "NY",
    "929": "NY",
    "934": "NY",
    "252": "NC",
    "336": "NC",
    "704": "NC",
    "743": "NC",
    "828": "NC",
    "910": "NC",
    "919": "NC",
    "980": "NC",
    "984": "NC",
    "701": "ND",
    "216": "OH",
    "220": "OH",
    "234": "OH",
    "330": "OH",
    "380": "OH",
    "419": "OH",
    "440": "OH",
    "513": "OH",
    "567": "OH",
    "614": "OH",
    "740": "OH",
    "937": "OH",
    "405": "OK",
    "539": "OK",
    "580": "OK",
    "918": "OK",
    "458": "OR",
    "503": "OR",
    "541": "OR",
    "971": "OR",
    "215": "PA",
    "223": "PA",
    "267": "PA",
    "272": "PA",
    "412": "PA",
    "445": "PA",
    "484": "PA",
    "570": "PA",
    "610": "PA",
    "717": "PA",
    "724": "PA",
    "814": "PA",
    "878": "PA",
    "401": "RI",
    "803": "SC",
    "839": "SC",
    "843": "SC",
    "854": "SC",
    "864": "SC",
    "605": "SD",
    "423": "TN",
    "615": "TN",
    "629": "TN",
    "731": "TN",
    "865": "TN",
    "901": "TN",
    "931": "TN",
    "210": "TX",
    "214": "TX",
    "254": "TX",
    "281": "TX",
    "325": "TX",
    "346": "TX",
    "361": "TX",
    "409": "TX",
    "430": "TX",
    "432": "TX",
    "469": "TX",
    "512": "TX",
    "682": "TX",
    "713": "TX",
    "726": "TX",
    "737": "TX",
    "806": "TX",
    "817": "TX",
    "830": "TX",
    "832": "TX",
    "903": "TX",
    "915": "TX",
    "936": "TX",
    "940": "TX",
    "956": "TX",
    "972": "TX",
    "979": "TX",
    "385": "UT",
    "435": "UT",
    "801": "UT",
    "802": "VT",
    "276": "VA",
    "434": "VA",
    "540": "VA",
    "571": "VA",
    "703": "VA",
    "757": "VA",
    "804": "VA",
    "206": "WA",
    "253": "WA",
    "360": "WA",
    "425": "WA",
    "509": "WA",
    "564": "WA",
    "202": "DC",
    "304": "WV",
    "681": "WV",
    "262": "WI",
    "414": "WI",
    "534": "WI",
    "608": "WI",
    "715": "WI",
    "920": "WI",
    "307": "WY",
}

# Toll-free and other non-geographic NANP codes (mirrors geo.ts NON_GEOGRAPHIC_CODES)
NON_GEOGRAPHIC_CODES: frozenset[str] = frozenset(
    {
        "800",
        "833",
        "844",
        "855",
        "866",
        "877",
        "888",  # toll-free
        "900",  # premium rate
        "456",  # inbound international
        "500",
        "521",
        "522",
        "523",
        "524",
        "525",
        "526",
        "527",
        "528",
        "529",
        "533",  # personal communications
        "700",  # interexchange carrier
    }
)

# Reasons returned by state_for_number when no state can be resolved.
REASON_OK = "OK"
REASON_INVALID = "INVALID_NUMBER"
REASON_NON_US = "NON_US_DESTINATION"
REASON_TOLL_FREE = "TOLL_FREE_OR_NON_GEOGRAPHIC"
REASON_UNKNOWN_NPA = "UNKNOWN_OR_UNSUPPORTED_AREA_CODE"


def normalize_us_e164(raw: str | None) -> str | None:
    """Normalize a US number in 10-digit / 11-digit / formatted / E.164 form to +1XXXXXXXXXX.

    Returns None when the input is not a plausible US NANP number:
    - fewer or more digits than a NANP number allows
    - NXX/NPA starting with 0 or 1
    - explicit non-+1 country code
    """
    if not raw:
        return None
    s = raw.strip()
    has_plus = s.startswith("+")
    digits = "".join(ch for ch in s if ch.isdigit())
    if has_plus and not digits.startswith("1"):
        return None  # explicit non-US country code
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    npa, nxx = digits[0:3], digits[3:6]
    if npa[0] in "01" or nxx[0] in "01":
        return None
    return "+1" + digits


def extract_npa(raw: str | None) -> str | None:
    e164 = normalize_us_e164(raw)
    if not e164:
        return None
    return e164[2:5]


def state_for_number(raw: str | None) -> tuple[str | None, str]:
    """Resolve the US state for a phone number.

    Returns (state, REASON_OK) on success, else (None, reason) where reason is one of
    REASON_INVALID / REASON_NON_US / REASON_TOLL_FREE / REASON_UNKNOWN_NPA.
    """
    if not raw:
        return None, REASON_INVALID
    s = raw.strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if s.startswith("+") and digits and not digits.startswith("1"):
        return None, REASON_NON_US
    e164 = normalize_us_e164(raw)
    if not e164:
        return None, REASON_INVALID
    npa = e164[2:5]
    if npa in NON_GEOGRAPHIC_CODES:
        return None, REASON_TOLL_FREE
    state = AREA_CODE_TO_STATE.get(npa)
    if not state:
        return None, REASON_UNKNOWN_NPA
    return state, REASON_OK
