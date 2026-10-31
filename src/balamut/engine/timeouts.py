"""The no-progress timeout policy of a mutant request."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

type CostView = Mapping[str, float]  # node id -> seconds

_CALIBRATED_TIMEOUT_MULTIPLIER = 2.0
_CALIBRATED_TIMEOUT_HEADROOM_S = 1.0


def _retry_timeouts_are_finite(base: float, multiplier: float, retries: int) -> bool:
    """The whole geometric sequence of retry timeouts stays finite and positive."""
    try:
        final_timeout = base * math.pow(multiplier, retries)
    except OverflowError:
        return False
    return math.isfinite(final_timeout) and final_timeout > 0


def timeout_policy_error(base: float, multiplier: float, retries: int) -> str | None:
    """Why the mutant timeout policy is invalid; None when it is valid. One rule for the CLI and the campaign."""
    if retries < 0:
        return "timeout retries cannot be negative"
    if not math.isfinite(base) or base <= 0:
        return "the mutant timeout must be a finite positive number"
    if not math.isfinite(multiplier) or multiplier <= 0:
        return "the timeout retry multiplier must be a finite positive number"
    if not _retry_timeouts_are_finite(base, multiplier, retries):
        return "the sequence of retry timeouts must stay finite and positive"
    return None


def hang_wall_budget(base: float, multiplier: float, retries: int) -> float:
    """The worst-case serial wall time of a hanging mutant: the sum of the timeout budgets of all attempts.

    The initial attempt plus every retry escalated by ``multiplier``: exactly the time a mutant hanging on each
    attempt burns, an honest weight for the ETA. A plain sum rather than the closed form, which divides by
    ``multiplier - 1``."""
    total = base
    budget = base
    for _ in range(retries):
        budget *= multiplier
        total += budget
    return total


def mutant_timeout_budget(base: float, nodeids: Iterable[str], costs: CostView) -> float:
    """Raise the no-progress budget to twice the baseline cost of the slowest covering test.

    The watchdog hears of a test only after its pytest phase finishes. A static budget shorter than the normal cost
    of a test produces a false TIMEOUT, restarts a green daemon and sends the mutant to a retry. One second of
    headroom absorbs scheduler noise; unknown, negative and non-finite costs do not affect the budget."""
    slowest = max(
        (
            seconds
            for nodeid in nodeids
            if (seconds := costs.get(nodeid)) is not None and math.isfinite(seconds) and seconds >= 0
        ),
        default=0.0,
    )
    return max(base, slowest * _CALIBRATED_TIMEOUT_MULTIPLIER + _CALIBRATED_TIMEOUT_HEADROOM_S)
