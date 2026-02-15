from abides_core.utils import str_to_ns


def test_str_to_ns():
    assert str_to_ns("0") == 0
    assert str_to_ns("1") == 1

    assert str_to_ns("1us") == 1e3
    assert str_to_ns("1ms") == 1e6

    assert str_to_ns("1s") == 1e9
    assert str_to_ns("1sec") == 1e9
    assert str_to_ns("1second") == 1e9

    assert str_to_ns("1m") == 1e9 * 60
    assert str_to_ns("1min") == 1e9 * 60
    assert str_to_ns("1minute") == 1e9 * 60

    assert str_to_ns("1h") == 1e9 * 60 * 60
    assert str_to_ns("1hr") == 1e9 * 60 * 60
    assert str_to_ns("1hour") == 1e9 * 60 * 60

    assert str_to_ns("1d") == 1e9 * 60 * 60 * 24
    assert str_to_ns("1day") == 1e9 * 60 * 60 * 24

    assert str_to_ns("00:00:00") == 0
    assert str_to_ns("00:00:01") == 1e9
    assert str_to_ns("00:01:00") == 1e9 * 60
    assert str_to_ns("01:00:00") == 1e9 * 60 * 60


def test_str_to_ns_uppercase_S():
    """Regression: uppercase 'S' (deprecated seconds alias) must be handled."""
    assert str_to_ns("10S") == 10_000_000_000
    assert str_to_ns("30S") == 30_000_000_000
    assert str_to_ns("60S") == 60_000_000_000
    assert str_to_ns("1S") == 1_000_000_000


def test_str_to_ns_returns_int():
    """The return type must always be a plain int for NanosecondTime."""
    assert isinstance(str_to_ns("1s"), int)
    assert isinstance(str_to_ns("1min"), int)
    assert isinstance(str_to_ns("10S"), int)
    assert isinstance(str_to_ns(42), int)
    assert isinstance(str_to_ns(0), int)


def test_str_to_ns_numeric_passthrough():
    """Numeric inputs are returned as-is (converted to int)."""
    assert str_to_ns(42) == 42
    assert str_to_ns(0) == 0
    assert str_to_ns(1_000_000_000) == 1_000_000_000
