"""Tests for per-lead dialing windows, local presence and retry spacing."""

from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from api.constants import DEFAULT_CAMPAIGN_RETRY_CONFIG
from api.services.campaign.dialing_windows import (
    dialing_config,
    extract_nanp_area_code,
    is_within_local_window,
    rank_from_numbers_by_locality,
    resolve_lead_timezone,
    resolve_retry_delay_seconds,
    seconds_until_local_window,
    timezone_for_phone_number,
)
from api.services.campaign.nanp_timezones import (
    NANP_AREA_CODE_TIMEZONES,
    duplicate_area_codes,
)

# Weekday business hours, Monday (0) through Friday (4).
BUSINESS_HOURS = [
    {"day_of_week": day, "start_time": "09:00", "end_time": "17:00"}
    for day in range(5)
]


def _utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class TestAreaCodeTable:
    def test_no_area_code_is_claimed_twice(self):
        # A duplicate means one group silently overwrote another and some
        # leads get the wrong local time.
        assert duplicate_area_codes() == {}

    def test_every_zone_resolves(self):
        for zone in set(NANP_AREA_CODE_TIMEZONES.values()):
            ZoneInfo(zone)

    @pytest.mark.parametrize(
        "area_code,expected",
        [
            ("212", "America/New_York"),
            ("312", "America/Chicago"),
            ("415", "America/Los_Angeles"),
            ("602", "America/Phoenix"),
            ("303", "America/Denver"),
            ("907", "America/Anchorage"),
            ("808", "Pacific/Honolulu"),
            ("416", "America/New_York"),
            ("604", "America/Los_Angeles"),
            ("306", "America/Regina"),
            ("787", "America/Puerto_Rico"),
        ],
    )
    def test_spot_checks(self, area_code, expected):
        assert NANP_AREA_CODE_TIMEZONES[area_code] == expected


class TestExtractAreaCode:
    @pytest.mark.parametrize(
        "number",
        ["+1 (212) 555-0100", "12125550100", "212-555-0100", "2125550100"],
    )
    def test_accepts_the_shapes_lead_lists_contain(self, number):
        assert extract_nanp_area_code(number) == "212"

    @pytest.mark.parametrize(
        "number", [None, "", "555", "+44 20 7946 0958", "0125550100", "1125550100"]
    )
    def test_rejects_what_is_not_a_nanp_number(self, number):
        assert extract_nanp_area_code(number) is None

    def test_unknown_area_code_has_no_timezone(self):
        assert timezone_for_phone_number("+1 (999) 555-0100") is None


class TestResolveLeadTimezone:
    def test_explicit_lead_timezone_wins(self):
        assert (
            resolve_lead_timezone(
                {"timezone": "America/Denver"}, "+12125550100", "UTC"
            )
            == "America/Denver"
        )

    @pytest.mark.parametrize("key", ["timezone", "time_zone", "lead_timezone"])
    def test_accepts_any_of_the_known_keys(self, key):
        assert (
            resolve_lead_timezone({key: "America/Denver"}, None, None)
            == "America/Denver"
        )

    def test_falls_back_to_the_area_code(self):
        assert (
            resolve_lead_timezone({}, "+12125550100", "America/Los_Angeles")
            == "America/New_York"
        )

    def test_falls_back_to_the_campaign_timezone(self):
        assert (
            resolve_lead_timezone({}, "+442079460958", "America/Los_Angeles")
            == "America/Los_Angeles"
        )

    def test_garbage_lead_timezone_is_ignored(self):
        assert (
            resolve_lead_timezone({"timezone": "Mars/Olympus"}, "+12125550100", None)
            == "America/New_York"
        )

    def test_returns_none_when_nothing_resolves(self):
        assert resolve_lead_timezone({}, "+442079460958", None) is None


class TestLocalWindow:
    def test_eastern_lead_is_inside_at_noon_eastern(self):
        # 16:00 UTC is noon in New York (DST).
        assert is_within_local_window(
            BUSINESS_HOURS, "America/New_York", _utc(2026, 6, 15, 16)
        )

    def test_eastern_lead_is_outside_when_a_pacific_campaign_would_dial(self):
        """The bug this exists to fix: 7am Eastern from a Pacific schedule."""
        moment = _utc(2026, 6, 15, 11)  # 07:00 New York, 04:00 Los Angeles
        assert not is_within_local_window(BUSINESS_HOURS, "America/New_York", moment)

    def test_weekend_is_outside(self):
        assert not is_within_local_window(
            BUSINESS_HOURS, "America/New_York", _utc(2026, 6, 20, 16)
        )

    @pytest.mark.parametrize(
        "slots,timezone",
        [(None, "America/New_York"), ([], "America/New_York"), (BUSINESS_HOURS, None)],
    )
    def test_fails_open(self, slots, timezone):
        assert is_within_local_window(slots, timezone, _utc(2026, 6, 15, 3))

    def test_unknown_timezone_fails_open(self):
        assert is_within_local_window(
            BUSINESS_HOURS, "Mars/Olympus", _utc(2026, 6, 15, 3)
        )


class TestSecondsUntilLocalWindow:
    def test_none_when_already_open(self):
        assert (
            seconds_until_local_window(
                BUSINESS_HOURS, "America/New_York", _utc(2026, 6, 15, 16)
            )
            is None
        )

    def test_waits_until_this_morning_opens(self):
        # 11:00 UTC is 07:00 New York; the window opens at 09:00, two hours on.
        wait = seconds_until_local_window(
            BUSINESS_HOURS, "America/New_York", _utc(2026, 6, 15, 11)
        )
        assert wait == pytest.approx(2 * 3600)

    def test_rolls_over_the_weekend(self):
        # Saturday afternoon -> Monday 09:00 New York.
        wait = seconds_until_local_window(
            BUSINESS_HOURS, "America/New_York", _utc(2026, 6, 20, 18)
        )
        assert wait is not None
        opens_at = _utc(2026, 6, 20, 18) + __import__("datetime").timedelta(
            seconds=wait
        )
        local = opens_at.astimezone(ZoneInfo("America/New_York"))
        assert (local.weekday(), local.hour, local.minute) == (0, 9, 0)

    def test_none_when_no_slot_ever_matches(self):
        assert (
            seconds_until_local_window(
                [{"day_of_week": 9, "start_time": "09:00", "end_time": "17:00"}],
                "America/New_York",
                _utc(2026, 6, 15, 11),
            )
            is None
        )

    def test_ignores_a_slot_with_an_unparseable_start(self):
        assert (
            seconds_until_local_window(
                [{"day_of_week": 0, "start_time": "not-a-time", "end_time": "17:00"}],
                "America/New_York",
                _utc(2026, 6, 15, 11),
            )
            is None
        )


class TestLocalPresence:
    def test_prefers_an_exact_area_code_match(self):
        ranked = rank_from_numbers_by_locality(
            ["+13105550100", "+12125550101", "+14155550102"], "+12125559999"
        )
        assert ranked[0] == "+12125550101"

    def test_then_prefers_the_same_timezone(self):
        ranked = rank_from_numbers_by_locality(
            ["+14155550102", "+16175550103"], "+12125559999"
        )
        # Boston shares Eastern with New York; San Francisco does not.
        assert ranked[0] == "+16175550103"

    def test_leaves_the_pool_alone_for_a_non_nanp_destination(self):
        pool = ["+13105550100", "+12125550101"]
        assert rank_from_numbers_by_locality(pool, "+442079460958") == pool

    def test_keeps_original_order_within_a_tier(self):
        pool = ["+14155550102", "+15105550103"]
        assert rank_from_numbers_by_locality(pool, "+12125559999") == pool

    def test_handles_an_empty_pool(self):
        assert rank_from_numbers_by_locality([], "+12125559999") == []


class TestRetryDelays:
    def test_uses_the_ladder_when_present(self):
        config = {"retry_delays_seconds": [1800, 10800, 86400]}
        assert resolve_retry_delay_seconds(config, 1) == 1800
        assert resolve_retry_delay_seconds(config, 2) == 10800
        assert resolve_retry_delay_seconds(config, 3) == 86400

    def test_clamps_past_the_end_of_the_ladder(self):
        config = {"retry_delays_seconds": [1800, 10800]}
        assert resolve_retry_delay_seconds(config, 9) == 10800

    def test_bare_delay_reproduces_the_old_behavior(self):
        config = {"retry_delay_seconds": 120}
        assert resolve_retry_delay_seconds(config, 1) == 120
        assert resolve_retry_delay_seconds(config, 2) == 120

    def test_daypart_shift_moves_later_attempts_to_another_time_of_day(self):
        config = {"retry_delay_seconds": 120, "daypart_shift_hours": 3}
        assert resolve_retry_delay_seconds(config, 1) == 120
        assert resolve_retry_delay_seconds(config, 2) == 120 + 3 * 3600
        assert resolve_retry_delay_seconds(config, 3) == 120 + 6 * 3600

    @pytest.mark.parametrize("attempt", [0, -5])
    def test_treats_a_bad_attempt_number_as_the_first(self, attempt):
        assert resolve_retry_delay_seconds({"retry_delay_seconds": 120}, attempt) == 120

    @pytest.mark.parametrize(
        "config",
        [
            {"retry_delays_seconds": ["oops"]},
            {"retry_delay_seconds": "oops"},
            {"retry_delay_seconds": 120, "daypart_shift_hours": "oops"},
            {},
        ],
    )
    def test_survives_malformed_config(self, config):
        assert resolve_retry_delay_seconds(config, 1) >= 0

    def test_default_campaign_config_spreads_attempts(self):
        delays = [
            resolve_retry_delay_seconds(DEFAULT_CAMPAIGN_RETRY_CONFIG, attempt)
            for attempt in (1, 2, 3)
        ]
        assert delays == sorted(delays)
        # Two minutes is not a retry, it is the same moment again.
        assert delays[0] >= 15 * 60


class TestDialingConfig:
    def test_defaults_when_nothing_is_configured(self):
        policy = dialing_config(SimpleNamespace(orchestrator_metadata=None))
        assert policy.schedule_enabled is False
        assert policy.per_lead_timezone is True
        assert policy.local_presence is True
        assert policy.from_number_daily_cap is None

    def test_schedule_is_only_enabled_with_slots(self):
        policy = dialing_config(
            SimpleNamespace(
                orchestrator_metadata={
                    "schedule_config": {"enabled": True, "slots": []}
                }
            )
        )
        assert policy.schedule_enabled is False

    def test_reads_the_dialing_block(self):
        policy = dialing_config(
            SimpleNamespace(
                orchestrator_metadata={
                    "schedule_config": {
                        "enabled": True,
                        "slots": BUSINESS_HOURS,
                        "timezone": "America/Chicago",
                    },
                    "dialing": {
                        "per_lead_timezone": False,
                        "local_presence": False,
                        "from_number_daily_cap": 50,
                    },
                }
            )
        )
        assert policy.schedule_enabled is True
        assert policy.timezone == "America/Chicago"
        assert policy.per_lead_timezone is False
        assert policy.local_presence is False
        assert policy.from_number_daily_cap == 50

    @pytest.mark.parametrize("cap", [0, -1, "oops", None])
    def test_rejects_a_nonsensical_cap(self, cap):
        policy = dialing_config(
            SimpleNamespace(
                orchestrator_metadata={"dialing": {"from_number_daily_cap": cap}}
            )
        )
        assert policy.from_number_daily_cap is None
