from api.services.campaign.source_sync import CampaignSourceSyncService as S


def test_phone_header_aliases_map_to_phone_number():
    assert S.normalize_headers(["Name", "Phone"]) == ["name", "phone_number"]
    assert S.normalize_headers(["﻿Phone Number", "x"])[0] == "phone_number"
    assert S.normalize_headers(["Mobile-Phone"])[0] == "phone_number"
    assert S.normalize_headers(["﻿phone_number"])[0] == "phone_number"


def test_existing_phone_number_header_wins_over_alias():
    assert S.normalize_headers(["Phone", "phone_number"]) == ["phone", "phone_number"]


def test_normalize_phone_number():
    assert S.normalize_phone_number("(415) 555-0132") == "+14155550132"
    assert S.normalize_phone_number("1-415-555-0132") == "+14155550132"
    assert S.normalize_phone_number("4155550132.0") == "+14155550132"
    assert S.normalize_phone_number("+44 20 7946 0958") == "+442079460958"
    assert S.normalize_phone_number("") == ""
    assert S.normalize_phone_number("12345") == "12345"


def test_validate_source_data_accepts_us_spreadsheet():
    result = S.validate_source_data(
        ["First Name", "Phone"],
        [["Ann", "(415) 555-0132"], ["Bob", "4155550133"]],
    )
    assert result.is_valid, result.error
    assert result.headers == ["first name", "phone_number"]
    assert result.rows[0][1] == "+14155550132"


def test_missing_phone_column_lists_found_columns():
    result = S.validate_source_data(["Name", "Email"], [["a", "b"]])
    assert not result.is_valid
    assert "'name', 'email'" in result.error.message


def test_duplicates_detected_after_normalization():
    result = S.validate_source_data(["phone"], [["4155550132"], ["+1 (415) 555-0132"]])
    assert not result.is_valid
    assert "Duplicate" in result.error.message
