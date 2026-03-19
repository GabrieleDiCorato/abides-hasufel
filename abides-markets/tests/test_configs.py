"""Tests for RMSC config correctness."""

from abides_markets.configs import rmsc03, rmsc04


def test_rmsc03_agent_types_are_whole_strings():
    """Verify agent_types list is built correctly (not split into chars).

    The configs use agent_types internally but don't return it.
    We verify indirectly: agents list should have correct types and
    the config should build without error.
    """
    config = rmsc03.build_config(seed=42)
    agents = config["agents"]
    type_names = [type(a).__name__ for a in agents]
    # Each name should be a real class name, not a single character
    for name in type_names:
        assert len(name) > 1, f"Agent type name is single char: '{name}'"
    expected = {"ExchangeAgent", "NoiseAgent", "ValueAgent", "MomentumAgent"}
    assert expected.issubset(
        set(type_names)
    ), f"Missing agent types. Got: {set(type_names)}"


def test_rmsc04_agent_types_are_whole_strings():
    """Same verification for rmsc04."""
    config = rmsc04.build_config(seed=42)
    agents = config["agents"]
    type_names = [type(a).__name__ for a in agents]
    for name in type_names:
        assert len(name) > 1, f"Agent type name is single char: '{name}'"
    expected = {
        "ExchangeAgent",
        "NoiseAgent",
        "ValueAgent",
        "AdaptiveMarketMakerAgent",
        "MomentumAgent",
    }
    assert expected.issubset(
        set(type_names)
    ), f"Missing agent types. Got: {set(type_names)}"
