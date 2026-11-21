"""Conservative EN/ES transcript normalizer used before WER/CER and field matching.

The same function is applied to references, hypotheses and critical-field
values, so any canonical form is fine as long as it is applied consistently.
Steps, in order (documented in README.md "Normalization"):

 1. Unicode NFKC; curly quotes -> straight.
 2. Upper-case two-letter US state codes in the original text ("TX") -> state
    name (skipped for "OK", and for all-caps text longer than two words).
 3. Lower-case; numeric dates ("3/3/1951", "1951-03-03") -> "march 3 1951"
    (EN, M/D/Y) or "3 de marzo de 1951" (ES, D/M/Y).
 4. Money: "$25,000" / "$25k" / "$25 thousand" -> "25000 dollar";
    "$25.50" -> "25 dollar 50 cent"; dollars/dólares -> dollar/dolar.
 5. Thousands separators, ordinal suffixes ("3rd" -> "3"), "%" -> percent.
 6. EN contractions expanded (n't -> not, 'm -> am, 're -> are, 've -> have,
    'll -> will, 'd -> would); "'s" is kept (ambiguous).
 7. Diacritics stripped (so "sí" and "si" compare equal -- documented loss).
 8. Hyphens -> spaces; other punctuation removed (a "." between digits kept).
 9. Hesitation fillers removed (EN: uh um uhm umm er erm hmm ah; ES: eh em mmm).
    Back-channels that carry polarity are kept, canonicalized ("uh huh" ->
    "uhhuh", "mm hmm" -> "mhm").
10. Spoken numbers -> digits (EN and basic ES cardinals, "a hundred",
    "veinticinco mil", years like "nineteen fifty one"); ordinal words ->
    numbers ("third" -> 3; "first"/"second"/"primero" only next to a month or
    "of"/"de" so "first name" is untouched).
11. EN "oh"/"o" between digits -> 0 (phone numbers).
12. Adjacent digit tokens are merged into one digit string, so "555-123-4567",
    "five five five one two three four five six seven" and "March 3, 1951" vs
    "march third nineteen fifty one" compare equal regardless of grouping.
    Side effect: a number/number boundary error counts as one token error.
13. Spelling variants: ok/okay -> okay, alright -> all right.

What it deliberately does NOT do: spelling correction, stemming, synonym
merging, removing "the/a", or guessing homophones. Anything not listed above
counts as an error.
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Static tables
# ---------------------------------------------------------------------------

US_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york",
    "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina",
    "SD": "south dakota", "TN": "tennessee", "TX": "texas", "UT": "utah",
    "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia",
}
# Codes that are far more often ordinary words when written upper case.
_AMBIGUOUS_STATE_CODES = {"OK"}

# Spanish exonyms (accent-stripped) -> canonical English state name.
ES_STATE_NAMES = {
    "nueva york": "new york", "nueva jersey": "new jersey", "nuevo mexico": "new mexico",
    "nuevo hampshire": "new hampshire", "carolina del norte": "north carolina",
    "carolina del sur": "south carolina", "dakota del norte": "north dakota",
    "dakota del sur": "south dakota", "virginia occidental": "west virginia",
    "pensilvania": "pennsylvania", "luisiana": "louisiana", "misisipi": "mississippi",
    "misuri": "missouri", "hawai": "hawaii", "kansas": "kansas", "tejas": "texas",
    "florida": "florida", "california": "california", "arizona": "arizona",
    "oregon": "oregon", "michigan": "michigan", "rhode island": "rhode island",
}

MONTHS_EN = ["january", "february", "march", "april", "may", "june", "july",
             "august", "september", "october", "november", "december"]
MONTHS_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
             "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
_MONTH_WORDS = set(MONTHS_EN) | set(MONTHS_ES) | {"setiembre"}

FILLERS = {
    "en": {"uh", "um", "uhm", "umm", "er", "erm", "hmm", "hm", "ah"},
    "es": {"eh", "em", "mmm", "hmm", "hm", "ehm"},
}

_CONTRACTIONS = [
    (re.compile(r"\bcan't\b"), "can not"),
    (re.compile(r"\bwon't\b"), "will not"),
    (re.compile(r"\bain't\b"), "is not"),
    (re.compile(r"n't\b"), " not"),
    (re.compile(r"'m\b"), " am"),
    (re.compile(r"'re\b"), " are"),
    (re.compile(r"'ve\b"), " have"),
    (re.compile(r"'ll\b"), " will"),
    (re.compile(r"'d\b"), " would"),
]

EN_ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
           "six": 6, "seven": 7, "eight": 8, "nine": 9}
EN_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
            "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
EN_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
           "seventy": 70, "eighty": 80, "ninety": 90}
EN_SCALES = {"thousand": 1000, "million": 1_000_000}
# Ordinal word -> cardinal word.
EN_ORDINALS = {
    "first": "one", "second": "two", "third": "three", "fourth": "four", "fifth": "five",
    "sixth": "six", "seventh": "seven", "eighth": "eight", "ninth": "nine", "tenth": "ten",
    "eleventh": "eleven", "twelfth": "twelve", "thirteenth": "thirteen",
    "fourteenth": "fourteen", "fifteenth": "fifteen", "sixteenth": "sixteen",
    "seventeenth": "seventeen", "eighteenth": "eighteen", "nineteenth": "nineteen",
    "twentieth": "twenty", "thirtieth": "thirty",
}
_CONTEXT_ONLY_ORDINALS = {"first", "second", "primero", "primera"}

ES_UNITS = {"cero": 0, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
            "seis": 6, "siete": 7, "ocho": 8, "nueve": 9}
# 10..29 are single words in Spanish.
ES_TEENS = {"diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15,
            "dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19,
            "veinte": 20, "veintiuno": 21, "veintiun": 21, "veintidos": 22, "veintitres": 23,
            "veinticuatro": 24, "veinticinco": 25, "veintiseis": 26, "veintisiete": 27,
            "veintiocho": 28, "veintinueve": 29}
ES_TENS = {"treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60,
           "setenta": 70, "ochenta": 80, "noventa": 90}
ES_HUNDREDS = {"cien": 100, "ciento": 100, "doscientos": 200, "doscientas": 200,
               "trescientos": 300, "trescientas": 300, "cuatrocientos": 400,
               "cuatrocientas": 400, "quinientos": 500, "quinientas": 500,
               "seiscientos": 600, "seiscientas": 600, "setecientos": 700,
               "setecientas": 700, "ochocientos": 800, "ochocientas": 800,
               "novecientos": 900, "novecientas": 900}
ES_SCALES = {"mil": 1000, "millon": 1_000_000, "millones": 1_000_000}

_DIGITS = re.compile(r"^\d+$")


def strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn"
    )


# ---------------------------------------------------------------------------
# Regex-level steps (operate on strings)
# ---------------------------------------------------------------------------


def _expand_state_codes(text: str) -> str:
    # An all-caps sentence ("IN THE MORNING") is not a list of state codes; a
    # bare code ("TX", e.g. a critical-field value) is.
    if text.isupper() and len(text.split()) > 2:
        return text

    def repl(m: re.Match) -> str:
        code = m.group(1)
        if code in US_STATES and code not in _AMBIGUOUS_STATE_CODES:
            return US_STATES[code]
        return code

    return re.sub(r"\b([A-Z]{2})\b", repl, text)


def _numeric_dates(text: str, lang: str) -> str:
    def month_name(n: int) -> str | None:
        if 1 <= n <= 12:
            return (MONTHS_ES if lang == "es" else MONTHS_EN)[n - 1]
        return None

    def fmt(day: int, month: int, year: str) -> str | None:
        name = month_name(month)
        if name is None or not 1 <= day <= 31:
            return None
        if lang == "es":
            return f" {day} de {name} de {int(year)} "
        return f" {name} {day} {int(year)} "

    def slash(m: re.Match) -> str:
        a, b, y = int(m.group(1)), int(m.group(2)), m.group(3)
        out = fmt(a, b, y) if lang == "es" else fmt(b, a, y)
        return out or m.group(0)

    def iso(m: re.Match) -> str:
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        return fmt(d, mo, y) or m.group(0)

    text = re.sub(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", iso, text)
    return re.sub(r"\b(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\b", slash, text)


def _parse_amount(raw: str, lang: str) -> tuple[int, int | None] | None:
    """'25,000' -> (25000, None); '25.50' -> (25, 50). Locale-aware separators."""
    if lang == "es":
        thousands, decimal = ".", ","
        # "25,000" written EN-style inside Spanish text is still common.
        if re.fullmatch(r"\d{1,3}(,\d{3})+", raw):
            thousands, decimal = ",", "."
    else:
        thousands, decimal = ",", "."
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", raw):
            thousands, decimal = ".", ","
    raw = raw.rstrip(".,")
    if re.fullmatch(rf"\d{{1,3}}(\{thousands}\d{{3}})+(\{decimal}\d{{1,2}})?", raw) or re.fullmatch(
        rf"\d+(\{decimal}\d{{1,2}})?", raw
    ):
        whole, _, frac = raw.replace(thousands, "").partition(decimal)
        cents = None
        if frac:
            cents = int(frac.ljust(2, "0"))
        return int(whole), cents
    return None


_SCALE_SUFFIX = {"k": 1000, "thousand": 1000, "mil": 1000, "million": 1_000_000,
                 "millon": 1_000_000, "millón": 1_000_000, "millones": 1_000_000, "m": 1_000_000}


def _money(text: str, lang: str) -> str:
    unit = "dolar" if lang == "es" else "dollar"

    def repl(m: re.Match) -> str:
        parsed = _parse_amount(m.group(1), lang)
        if parsed is None:
            return m.group(0)
        whole, cents = parsed
        scale = m.group(2)
        if scale:
            mult = _SCALE_SUFFIX[scale.strip().lower()]
            value = whole * mult + (cents or 0) * mult // 100
            return f" {value} {unit} "
        if cents:
            return f" {whole} {unit} {cents} cent "
        return f" {whole} {unit} "

    # Currency word after the amount ("$25 dollars", "25.000 dólares") is dropped
    # by the trailing optional group so it is not doubled.
    text = re.sub(
        r"\$\s?(\d[\d,.]*)(\s?(?:k|thousand|million|mil|millones|millón|millon|m)\b)?"
        r"(?:\s?(?:dollars?|d[oó]lares|d[oó]lar|usd)\b)?",
        repl,
        text,
    )
    text = re.sub(r"\b(\d+)k\b", lambda m: str(int(m.group(1)) * 1000), text)
    text = re.sub(r"\bd[oó]lares\b|\bd[oó]lar\b", "dolar", text)
    text = re.sub(r"\bdollars\b|\busd\b", "dollar", text)
    text = re.sub(r"\bcents\b", "cent", text)
    text = re.sub(r"\bcentavos?\b", "cent", text)
    return text


def _separators_and_suffixes(text: str, lang: str) -> str:
    if lang == "es":
        text = re.sub(r"(?<=\d)\.(?=\d{3}\b)", "", text)
        text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
        text = re.sub(r"\b(\d+)\s?(?:º|ª|o|a)\b(?=\s+de\b)", r"\1", text)
        text = text.replace("%", " por ciento ")
    else:
        text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
        text = re.sub(r"\b(\d+)(st|nd|rd|th)\b", r"\1", text)
        text = text.replace("%", " percent ")
    return text


def _contractions(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'")
    for pattern, repl in _CONTRACTIONS:
        text = pattern.sub(repl, text)
    return text


def _tokenize(text: str) -> list[str]:
    text = text.replace("-", " ").replace("–", " ").replace("—", " ").replace("/", " ")
    text = re.sub(r"(?<=\d)\.(?=\d)", "<DOT>", text)
    text = re.sub(r"[^\w\s<>]", " ", text)
    text = text.replace("_", " ").replace("<DOT>", ".")
    return text.split()


# ---------------------------------------------------------------------------
# Token-level number parsing
# ---------------------------------------------------------------------------


def _ordinals_to_cardinals(tokens: list[str], lang: str) -> list[str]:
    out = list(tokens)
    for i, tok in enumerate(tokens):
        prev = tokens[i - 1] if i > 0 else ""
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok in _CONTEXT_ONLY_ORDINALS:
            in_date = prev in _MONTH_WORDS or nxt in ("of", "de") or nxt in _MONTH_WORDS
            if not in_date:
                continue
            out[i] = "uno" if lang == "es" else EN_ORDINALS.get(tok, tok)
        elif lang == "en" and tok in EN_ORDINALS:
            out[i] = EN_ORDINALS[tok]
    return out


def _en_kind(tok: str) -> str | None:
    if tok in EN_ONES:
        return "ones"
    if tok in EN_TEENS:
        return "teens"
    if tok in EN_TENS:
        return "tens"
    if tok == "hundred":
        return "hundred"
    if tok in EN_SCALES:
        return "scale"
    return None


def _parse_en_number(tokens: list[str], i: int) -> tuple[int, int] | None:
    """Parse one cardinal starting at tokens[i]; return (value, next index)."""
    n = len(tokens)
    total = current = 0
    last: str | None = None
    j = i
    started = False
    while j < n:
        tok = tokens[j]
        kind = _en_kind(tok)
        if not started and tok == "a" and j + 1 < n and tokens[j + 1] in ("hundred", "thousand", "million"):
            current, last, started, j = 1, "ones", True, j + 1
            continue
        if not started and _DIGITS.match(tok) and j + 1 < n and tokens[j + 1] in ("hundred", "thousand", "million"):
            current, last, started, j = int(tok), "ones", True, j + 1
            continue
        if kind == "ones":
            if last in ("ones", "teens"):
                break
            current += EN_ONES[tok]
        elif kind in ("teens", "tens"):
            if last in ("ones", "teens", "tens"):
                break
            current += EN_TEENS.get(tok) or EN_TENS[tok]
        elif kind == "hundred":
            if last not in ("ones", "teens", "tens", None) or current >= 100 or not started and j > i:
                break
            current = (current or 1) * 100
        elif kind == "scale":
            if last == "scale" or not started:
                break
            scale = EN_SCALES[tok]
            if total and total < scale:
                total = (total + (current or 1)) * scale
            else:
                total += (current or 1) * scale
            current = 0
        elif tok == "and" and last in ("hundred", "scale") and j + 1 < n and _en_kind(tokens[j + 1]) in ("ones", "teens", "tens"):
            j += 1
            continue
        else:
            break
        last = kind
        started = True
        j += 1
    if not started:
        return None
    return total + current, j


def _es_kind(tok: str) -> str | None:
    if tok in ES_UNITS:
        return "units"
    if tok in ES_TEENS:
        return "teens"
    if tok in ES_TENS:
        return "tens"
    if tok in ES_HUNDREDS:
        return "hundreds"
    if tok in ES_SCALES:
        return "scale"
    return None


def _parse_es_number(tokens: list[str], i: int) -> tuple[int, int] | None:
    n = len(tokens)
    total = current = 0
    last: str | None = None
    started = False
    j = i
    while j < n:
        tok = tokens[j]
        kind = _es_kind(tok)
        if not started and tok in ("un", "una") and j + 1 < n and tokens[j + 1] in ES_SCALES:
            current, last, started, j = 1, "units", True, j + 1
            continue
        if not started and _DIGITS.match(tok) and j + 1 < n and tokens[j + 1] in ES_SCALES:
            current, last, started, j = int(tok), "units", True, j + 1
            continue
        if kind == "units":
            # A unit after a tens word needs "y" ("treinta y cinco").
            if last in ("units", "teens", "tens"):
                break
            current += ES_UNITS[tok]
        elif kind == "teens":
            if last in ("units", "teens", "tens"):
                break
            current += ES_TEENS[tok]
        elif kind == "tens":
            if last in ("units", "teens", "tens"):
                break
            current += ES_TENS[tok]
        elif kind == "hundreds":
            if last in ("units", "teens", "tens", "hundreds"):
                break
            current += ES_HUNDREDS[tok]
        elif kind == "scale":
            if not started and tok != "mil":
                break
            scale = ES_SCALES[tok]
            if last == "scale" and scale == 1000 and total % 1_000_000 == 0 and current == 0 and total:
                break
            if scale == 1000:
                total += (current or 1) * 1000
            else:
                total = (total + (current or 1)) * scale if total and total < scale else total + (current or 1) * scale
            current = 0
        elif tok == "y" and last == "tens" and j + 1 < n and tokens[j + 1] in ES_UNITS:
            current += ES_UNITS[tokens[j + 1]]
            last = "units"
            j += 2
            continue
        else:
            break
        last = kind
        started = True
        j += 1
    if not started:
        return None
    return total + current, j


def _words_to_numbers(tokens: list[str], lang: str) -> list[str]:
    tokens = _ordinals_to_cardinals(tokens, lang)
    parse = _parse_es_number if lang == "es" else _parse_en_number
    out: list[str] = []
    i = 0
    while i < len(tokens):
        parsed = parse(tokens, i)
        if parsed is not None and parsed[1] > i:
            value, j = parsed
            out.append(str(value))
            i = j
        else:
            out.append(tokens[i])
            i += 1
    return out


def _oh_as_zero(tokens: list[str]) -> list[str]:
    out = list(tokens)
    for i in range(1, len(tokens) - 1):
        if tokens[i] in ("oh", "o") and _DIGITS.match(out[i - 1]) and _DIGITS.match(tokens[i + 1]):
            out[i] = "0"
    return out


def _merge_digit_runs(tokens: list[str]) -> list[str]:
    out: list[str] = []
    for tok in tokens:
        if _DIGITS.match(tok) and out and _DIGITS.match(out[-1]):
            out[-1] += tok
        else:
            out.append(tok)
    return out


def _es_state_names(text: str) -> str:
    for es, en in sorted(ES_STATE_NAMES.items(), key=lambda kv: -len(kv[0])):
        if es != en:
            text = re.sub(rf"\b{es}\b", en, text)
    return text


def _backchannels(tokens: list[str]) -> list[str]:
    """'uh huh' -> 'uhhuh', 'mm hmm' / 'mmhmm' -> 'mhm' (before fillers are removed)."""
    out: list[str] = []
    i = 0
    while i < len(tokens):
        pair = tuple(tokens[i:i + 2])
        if pair in (("uh", "huh"), ("uh", "hu")):
            out.append("uhhuh")
            i += 2
        elif pair in (("mm", "hmm"), ("mm", "hm"), ("m", "hm")):
            out.append("mhm")
            i += 2
        else:
            out.append({"mmhmm": "mhm", "mmhm": "mhm", "uhuh": "uhhuh"}.get(tokens[i], tokens[i]))
            i += 1
    return out


def _drop_money_conjunction(tokens: list[str]) -> list[str]:
    """'25 dollar and 50 cent' / '25 dolar con 50 cent' -> '25 dollar 50 cent'."""
    out: list[str] = []
    for i, tok in enumerate(tokens):
        if (
            tok in ("and", "y", "con")
            and out
            and out[-1] in ("dollar", "dolar")
            and i + 1 < len(tokens)
            and _DIGITS.match(tokens[i + 1])
        ):
            continue
        out.append(tok)
    return out


_SPELLING = {"ok": "okay", "alright": "all right", "oke": "okay"}


def normalize_tokens(text: str | None, language: str = "en") -> list[str]:
    """Normalize ``text`` and return its tokens (see module docstring)."""
    if not text:
        return []
    lang = "es" if (language or "en").lower().startswith("es") else "en"
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = _expand_state_codes(text)
    text = text.lower()
    text = _numeric_dates(text, lang)
    text = _money(text, lang)
    text = _separators_and_suffixes(text, lang)
    if lang == "en":
        text = _contractions(text)
    text = strip_accents(text)
    if lang == "es":
        text = _es_state_names(text)
    tokens = _backchannels(_tokenize(text))
    fillers = FILLERS[lang]
    tokens = [t for t in tokens if t not in fillers]
    tokens = _words_to_numbers(tokens, lang)
    if lang == "en":
        tokens = _oh_as_zero(tokens)
    tokens = _merge_digit_runs(tokens)
    tokens = _drop_money_conjunction(tokens)
    out: list[str] = []
    for t in tokens:
        out.extend(_SPELLING.get(t, t).split())
    return out


def normalize(text: str | None, language: str = "en") -> str:
    return " ".join(normalize_tokens(text, language))
