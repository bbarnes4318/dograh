"""Normalizer cases (EN/ES): numbers, money, dates, phone digits, states."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bench.normalize import normalize  # noqa: E402

EQUIVALENT = [
    # (language, a, b) -- must normalize identically
    ("en", "Twenty five thousand dollars", "$25,000"),
    ("en", "twenty-five thousand dollars", "$25k"),
    ("en", "$25.50", "twenty five dollars and fifty cents"),
    ("en", "$1,250,000", "one million two hundred fifty thousand dollars"),
    ("en", "My date of birth is March 3rd, 1951.", "my date of birth is march third nineteen fifty one"),
    ("en", "3/3/1951", "March 3, 1951"),
    ("en", "1951-03-03", "march third nineteen fifty-one"),
    ("en", "555-123-4567", "five five five one two three four five six seven"),
    ("en", "(555) 123-4567", "five fifty five one twenty three forty five sixty seven"),
    ("en", "5501", "five five oh one"),
    ("en", "I live in TX", "I live in Texas."),
    ("en", "I'm sixty-five years old", "I am 65 years old"),
    ("en", "I don't want it", "I do not want it."),
    ("en", "a hundred and five", "105"),
    ("en", "two thousand five", "2005"),
    ("en", "June 2nd", "June second"),
    ("en", "Um, it's uh fine", "it's fine"),
    ("en", "OK", "okay"),
    ("es", "veinticinco mil dólares", "$25.000"),
    ("es", "veinticinco mil dolares", "$25,000"),
    ("es", "veinticinco dólares con cincuenta centavos", "$25,50"),
    ("es", "Tengo sesenta y cinco años", "tengo 65 años"),
    ("es", "tres de abril de mil novecientos cincuenta y uno", "3/4/1951"),
    ("es", "primero de marzo", "1 de marzo"),
    ("es", "Vivo en Nueva York", "vivo en NY"),
    ("es", "Sí", "si"),
    ("es", "cinco cinco cinco uno dos tres cuatro", "555-1234"),
    ("es", "dos mil cinco", "2005"),
    ("es", "un millón doscientos mil", "1.200.000"),
]


@pytest.mark.parametrize("lang,a,b", EQUIVALENT)
def test_equivalent_forms(lang, a, b):
    assert normalize(a, lang) == normalize(b, lang)


@pytest.mark.parametrize("lang,text,expected", [
    ("en", "Hello, World!", "hello world"),
    ("en", "$25,000", "25000 dollar"),
    ("en", "I live in TX", "i live in texas"),
    ("en", "OK, I am in OR", "okay i am in oregon"),  # OK not a state code
    ("en", "first name is John", "first name is john"),  # "first" untouched outside dates
    ("en", "the twenty first of May", "the 21 of may"),
    ("en", "two point five percent", "2 point 5 percent"),  # no decimal speech parsing (documented)
    ("es", "mil novecientos cincuenta y uno", "1951"),
    ("es", "No, no estoy interesado", "no no estoy interesado"),
    ("es", "un seguro", "un seguro"),  # article "un" is not a number
    ("en", "", ""),
])
def test_exact(lang, text, expected):
    assert normalize(text, lang) == expected


@pytest.mark.parametrize("lang,a,b", [
    ("en", "fifteen", "fifty"),
    ("en", "I am not interested", "I am interested"),
    ("en", "$25,000", "$2,500"),
    ("es", "sesenta y cinco", "setenta y cinco"),
    ("en", "Texas", "Tennessee"),
])
def test_conservative_does_not_merge_different_things(lang, a, b):
    assert normalize(a, lang) != normalize(b, lang)


def test_all_upper_text_does_not_expand_state_codes():
    assert normalize("IN THE MORNING", "en") == "in the morning"
