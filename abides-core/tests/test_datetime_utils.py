"""Tests for datetime utility functions in abides_core.utils.

These functions are currently untested and are critical for simulation timing.
"""

from abides_core.utils import datetime_str_to_ns, fmt_ts, ns_date, str_to_ns


class TestDatetimeStrToNs:
    """datetime_str_to_ns converts date strings to nanosecond Unix timestamps."""

    def test_compact_date(self):
        # 2021-02-05 00:00:00 UTC
        assert datetime_str_to_ns("20210205") == 1_612_483_200_000_000_000

    def test_iso_date(self):
        assert datetime_str_to_ns("2021-02-05") == 1_612_483_200_000_000_000

    def test_datetime_with_time(self):
        # 2021-02-05 09:30:00 UTC
        assert datetime_str_to_ns("2021-02-05 09:30:00") == 1_612_517_400_000_000_000

    def test_return_type(self):
        assert isinstance(datetime_str_to_ns("20210205"), int)

    def test_different_date(self):
        # 2020-06-03 00:00:00 UTC (used in rmsc03 config)
        assert datetime_str_to_ns("20200603") == 1_591_142_400_000_000_000


class TestFmtTs:
    """fmt_ts converts nanosecond timestamps back to human-readable strings."""

    def test_roundtrip_date(self):
        ns = datetime_str_to_ns("2021-02-05 09:30:00")
        assert fmt_ts(ns) == "2021-02-05 09:30:00"

    def test_midnight(self):
        ns = datetime_str_to_ns("2021-02-05")
        assert fmt_ts(ns) == "2021-02-05 00:00:00"

    def test_known_value(self):
        assert fmt_ts(1_612_517_400_000_000_000) == "2021-02-05 09:30:00"


class TestNsDate:
    """ns_date rounds a nanosecond timestamp down to midnight of that day."""

    def test_rounds_to_midnight(self):
        ns_datetime = datetime_str_to_ns("2021-02-05 09:30:00")
        ns_midnight = datetime_str_to_ns("2021-02-05")
        assert ns_date(ns_datetime) == ns_midnight

    def test_midnight_is_noop(self):
        ns_midnight = datetime_str_to_ns("2021-02-05")
        assert ns_date(ns_midnight) == ns_midnight

    def test_date_arithmetic_consistency(self):
        """Verify mkt_open/mkt_close arithmetic used in configs."""
        date = datetime_str_to_ns("20210205")
        mkt_open = date + str_to_ns("09:30:00")
        mkt_close = date + str_to_ns("16:00:00")

        # The date portion should round back to midnight
        assert ns_date(mkt_open) == date
        assert ns_date(mkt_close) == date

        # The market session duration should be 6.5 hours
        assert mkt_close - mkt_open == str_to_ns("6h") + str_to_ns("30min")
