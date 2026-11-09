"""Prices and the estimate."""

import re

import pytest

from auditkit import cost
from auditkit.cost import DEFAULT_MODEL, PRICES, Price, UnknownModel, estimate, trial_tokens
from auditkit.schema import EstimateHints

PHQ9_HINTS = EstimateHints(
    exchanges=14, tokens_per_exchange=120, output_tokens_per_call=150, system_tokens=1500
)


def test_prices_are_dated_and_sourced():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", cost.PRICES_AS_OF)
    assert cost.PRICES_SOURCE.startswith("https://")
    assert PRICES["claude-haiku-5-5"] == Price(0.10, 0.50)
    assert PRICES["claude-sonnet-5-5"] == Price(2.00, 10.00)


def test_default_model_is_haiku_and_priced():
    assert DEFAULT_MODEL == "claude-haiku-5-5"
    assert DEFAULT_MODEL in PRICES


def test_trial_tokens_resend_the_system_prompt_and_grow_the_conversation():
    hints = EstimateHints(
        exchanges=3, tokens_per_exchange=10, output_tokens_per_call=7, system_tokens=100
    )
    assert trial_tokens(hints, 3) == (3 * 100 + 10 * (1 + 2 + 3), 3 * 7)


def test_phq9_hints_cost_about_9_cents_per_trial_at_1_and_5_dollars(monkeypatch):
    monkeypatch.setitem(PRICES, "reference-model", Price(1.0, 5.0))
    models = {"agent": "reference-model", "simulator": "reference-model"}
    roles = estimate(PHQ9_HINTS, trials=1, max_turns=24, models=models)
    assert sum(r.typical_usd for r in roles) == pytest.approx(0.088, abs=5e-4)


def test_a_20_by_3_run_on_haiku_is_about_half_a_dollar():
    roles = estimate(PHQ9_HINTS, trials=20 * 3, max_turns=24, models={})
    assert [r.model for r in roles] == [DEFAULT_MODEL, DEFAULT_MODEL]
    assert sum(r.typical_usd for r in roles) == pytest.approx(0.53, abs=0.01)


def test_worst_case_uses_max_turns():
    (agent, _) = estimate(PHQ9_HINTS, trials=1, max_turns=24, models={})
    assert agent.worst_usd > agent.typical_usd


def test_a_cap_below_the_typical_exchanges_shortens_the_typical_trial():
    (agent, _) = estimate(PHQ9_HINTS, trials=1, max_turns=2, models={})
    assert agent.typical_usd == agent.worst_usd


def test_unknown_model_is_an_error():
    with pytest.raises(UnknownModel, match="no price for model 'claude-unknown'"):
        estimate(PHQ9_HINTS, trials=1, max_turns=24, models={"agent": "claude-unknown"})
