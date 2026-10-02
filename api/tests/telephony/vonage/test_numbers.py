import pytest

from api.services.telephony.providers.vonage.numbers import (
    VonagePhoneNumberError,
    from_vonage_number,
    to_e164,
    to_vonage_number,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+14155551212", "14155551212"),
        ("14155551212", "14155551212"),
        ("+1 (415) 555-1212", "14155551212"),
        ("(415) 555-1212", "14155551212"),
        ("415.555.1212", "14155551212"),
        ("4155551212", "14155551212"),
        ("+447700900123", "447700900123"),
        ("00447700900123", "447700900123"),
        ("  +14155551212  ", "14155551212"),
    ],
)
def test_to_vonage_number_accepts_valid_inputs(raw, expected):
    assert to_vonage_number(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "abc",
        "+1415555121a",
        "12345",  # too short
        "+1234567890123456",  # too long (16 digits)
        "1234567890",  # 10 digits, not a valid NANP number
        "0155551212",  # leading zero / no country code
        "+0447700900123",
        "sip:alice@example.com",
        "alice@example.com",
        "555-1212",
    ],
)
def test_to_vonage_number_rejects_invalid_inputs(raw):
    with pytest.raises(VonagePhoneNumberError):
        to_vonage_number(raw)


def test_error_names_the_field():
    with pytest.raises(VonagePhoneNumberError, match="caller ID"):
        to_vonage_number("abc", field="caller ID")


def test_to_e164_is_canonical_dograh_form():
    assert to_e164("14155551212") == "+14155551212"
    assert to_e164("+1 415 555 1212") == "+14155551212"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("14155551212", "+14155551212"),
        ("447700900123", "+447700900123"),
        ("Unknown", "Unknown"),
        ("sip:alice@example.com", "sip:alice@example.com"),
        ("", ""),
        (None, ""),
    ],
)
def test_from_vonage_number_never_invents_numbers(raw, expected):
    assert from_vonage_number(raw) == expected
