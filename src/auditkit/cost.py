"""Prices and the cost estimate. No model client here: `--lint` estimates without one.

Prices are USD per million tokens, as of PRICES_AS_OF, from PRICES_SOURCE. Both models think by
default and bill thinking as output; the estimate does not count it (a live run calibrates the
pack's hints).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from auditkit.schema import EstimateHints

PRICES_AS_OF = "2026-10-06"
PRICES_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
DEFAULT_MODEL = "claude-haiku-5-5"  # every role: the cheapest model that runs a pack
ROLES = ("agent", "simulator")  # the judge is designed, not built


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float


PRICES: dict[str, Price] = {
    # prompts up to 100K tokens; above, Haiku 5.5 costs 0.50 / 2.50 (a trial's prompts are smaller)
    "claude-haiku-5-5": Price(0.10, 0.50),
    "claude-sonnet-5-5": Price(2.00, 10.00),  # the fallback when Haiku proves too weak for a pack
}


class UnknownModel(Exception):
    """A model with no price: the estimate, and the budget cap, would be meaningless."""


@dataclass(frozen=True)
class RoleEstimate:
    role: str
    model: str
    typical_usd: float  # E = the pack's typical exchanges
    worst_usd: float  # E = max_turns


def price(model: str) -> Price:
    if model not in PRICES:
        known = ", ".join(sorted(PRICES))
        raise UnknownModel(f"no price for model {model!r} (known: {known}; as of {PRICES_AS_OF})")
    return PRICES[model]


def trial_tokens(hints: EstimateHints, exchanges: int) -> tuple[int, int]:
    """(input, output) tokens of one role over one trial of E exchanges: the system prompt is resent
    every call, and the conversation grows by T tokens per exchange."""
    e = exchanges
    return e * hints.system_tokens + hints.tokens_per_exchange * e * (e + 1) // 2, (
        e * hints.output_tokens_per_call
    )


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    p = price(model)
    return (input_tokens * p.input_per_mtok + output_tokens * p.output_per_mtok) / 1_000_000


def estimate(
    hints: EstimateHints, *, trials: int, max_turns: int, models: Mapping[str, str]
) -> list[RoleEstimate]:
    """The run's cost per role, typical and worst case, for `trials` = cases x repeats."""
    out = []
    for role in ROLES:
        model = models.get(role, DEFAULT_MODEL)
        typical_exchanges = min(hints.exchanges, max_turns)  # a lower cap shortens every trial
        typical = cost_usd(model, *trial_tokens(hints, typical_exchanges)) * trials
        worst = cost_usd(model, *trial_tokens(hints, max_turns)) * trials
        out.append(RoleEstimate(role, model, typical, worst))
    return out
