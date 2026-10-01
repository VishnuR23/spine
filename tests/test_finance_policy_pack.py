"""The finance policy pack, evaluated by the real policy engine, offline.

Pins what each action type the C++ gate can send actually gets, so a change
to the pack that lets a restricted amend through, or traps a cancel, fails
here rather than on a trading desk.
"""

import datetime as dt
import importlib.util
import pathlib

import pytest

from spine.core.policy_engine import decide, match_policy
from spine.schemas.intercept import AgentAction

_PACK_PATH = pathlib.Path(__file__).resolve().parents[1] / "examples" / "finance" / "seed_finance_policies.py"
_spec = importlib.util.spec_from_file_location("seed_finance_policies", _PACK_PATH)
pack = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pack)

IN_HOURS = dt.datetime(2026, 9, 30, 15, 0, tzinfo=dt.timezone.utc)
OUT_OF_HOURS = dt.datetime(2026, 9, 30, 23, 0, tzinfo=dt.timezone.utc)


def verdict(action_type: str, target: str, now: dt.datetime) -> tuple[str, str]:
    action = AgentAction(action_type=action_type, target_resource=target)
    matches = []
    for policy in pack.policies(always_open=False):
        m = match_policy(policy["name"], policy["rule_config"], action, now=now)
        if m is not None:
            matches.append(m)
    decision, _allowed, reason = decide(matches)
    return decision, reason


@pytest.mark.parametrize(
    "action_type, target, expected",
    [
        ("order.place", "AAPL", "allowed"),
        ("order.place", "RSTR", "blocked"),
        ("order.place.large", "AAPL", "flagged"),
        ("order.amend", "AAPL", "allowed"),
        ("order.amend", "RSTR", "blocked"),
        ("order.amend.large", "AAPL", "flagged"),
        ("order.amend.unpriced", "AAPL", "flagged"),
        ("order.amend.large", "RSTR", "blocked"),
        ("order.cancel", "AAPL", "allowed"),
        ("order.cancel", "RSTR", "allowed"),
    ],
)
def test_in_hours(action_type, target, expected):
    assert verdict(action_type, target, IN_HOURS)[0] == expected


def test_amend_above_the_ceiling_is_refused_by_the_ceiling_policy():
    decision, reason = verdict("order.amend.block", "AAPL", IN_HOURS)
    assert decision == "blocked"
    assert "Notional ceiling" in reason


def test_out_of_hours_amends_are_refused_by_the_hours_policy():
    decision, reason = verdict("order.amend", "AAPL", OUT_OF_HOURS)
    assert decision == "blocked"
    assert "outside trading hours" in reason


def test_out_of_hours_cancels_still_go_through():
    assert verdict("order.cancel", "AAPL", OUT_OF_HOURS)[0] == "allowed"
