"""make demo seeds a trading desk: the finance policy pack and its mandate."""

from tests.test_finance_policy_pack import pack
from tools import quickstart


def test_demo_seeds_the_finance_pack_open_around_the_clock():
    # 24h window, so the demo behaves the same outside market hours.
    assert quickstart.DEMO_POLICIES == pack.policies(always_open=True)


def test_demo_declares_the_trading_mandate():
    assert quickstart.DEMO_MANDATE == pack.MANDATE
