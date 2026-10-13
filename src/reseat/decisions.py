"""The models behind re:Seat's decisions, each with its source.

docs/models.md explains them in plain words. Every number here that is a judgement, not data, is named
as one: a prior, a weight or a relative scale. Nothing here calls the Events API.

Rules this module exists to respect:
- Honest uncertainty. Walk-up odds come with an interval, and the interval is wide when re:Seat has
  seen little. Never a bare yes or no.
- No invented counts. The API gives seat bands, not seats, and no room capacity. The models use only
  the band, the session type, the attendee's own priority and re:Seat's own sweep history.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from statistics import NormalDist

# ---------------------------------------------------------------------- walk-up odds
#
# Beta-binomial. Each time a session filled (its band went to unavailable), it either freed a seat
# again (a success) or it did not. With a Beta(a0, b0) prior from the session type, the posterior after
# s successes and f failures is Beta(a0 + s, b0 + f): mean a / (a + b), variance
# mean (1 - mean) / (a + b + 1). The interval is the normal approximation to that Beta, clipped to
# [0, 1]: pseudo-counts keep small-sample intervals honest (Agresti and Coull 1998).

PRIOR_ODDS = {"likely": 0.7, "early": 0.45, "unlikely": 0.15}   # judgement: the type rule as a rate
PRIOR_WEIGHT = 4.0       # judgement: the type rule counts as 4 observations of this session
Z80 = 1.2816             # an 80% two-sided interval
_OPEN = {"available", "limited", "veryLimited"}   # walkUp takes no reservation, so it frees no seat

History = Iterable[tuple[float, str | None, str | None]]


@dataclass(frozen=True)
class Odds:
    p: float             # posterior mean probability that a walk-up gets in
    lo: float
    hi: float
    fills: int           # times this session was seen to fill
    freed: int           # of those, times a seat freed again


def fill_episodes(history: History) -> tuple[int, int]:
    """(fills, freed): band changes into unavailable that re:Seat saw happen, and how many were later
    followed by an open band. The first sighting (old None) only sets the state: a session already
    full when first seen is not evidence that it filled. Each fill counts once, so a session that
    flaps is not over-counted."""
    fills = freed = 0
    full = counted = False      # counted: the current full spell is a fill re:Seat saw happen
    for _ts, old, new in sorted(history, key=lambda h: h[0]):
        if old is None:
            full, counted = new == "unavailable", False
        elif new == "unavailable" and not full:
            fills, full, counted = fills + 1, True, True
        elif full and new in _OPEN:
            freed += 1 if counted else 0
            full = counted = False
    return fills, freed


def walk_up_odds(prior: float | None, history: History) -> Odds:
    """The chance a walk-up is admitted, with an 80% interval. `prior` is the session type's rate,
    or None when there is no rule for the type (then a flat 0.5 with the same weight)."""
    fills, freed = fill_episodes(history)
    p0 = 0.5 if prior is None else prior
    a = p0 * PRIOR_WEIGHT + freed
    b = (1 - p0) * PRIOR_WEIGHT + (fills - freed)
    mean = a / (a + b)
    half = Z80 * math.sqrt(mean * (1 - mean) / (a + b + 1))
    return Odds(mean, max(0.0, mean - half), min(1.0, mean + half), fills, freed)


def verdict(o: Odds, known: bool) -> str:
    """The word for the page, from the posterior mean. Unknown when there is no type rule and
    nothing has been seen."""
    if not known and o.fills == 0:
        return "unknown"
    if o.p >= 0.6:
        return "likely"
    return "early" if o.p >= 0.35 else "unlikely"


def percent(o: Odds) -> str:
    return f"about {round(o.p * 100)}% ({round(o.lo * 100)} to {round(o.hi * 100)}%)"


# ---------------------------------------------------------------------- walking time allowance
#
# Arriving on time (Fan, Kalaba and Moore, JOTA 127(3), 2005): leave so that the chance of arriving
# before the deadline is at least alpha. For one route this is the alpha-quantile of the travel time,
# the planning time behind the FHWA buffer index (FHWA, Travel Time Reliability, 2005). Walking speeds
# vary around their mean with a standard deviation of 19.3 percent (Weidmann, Transporttechnik der
# Fussgaenger, ETH Zurich, 1993). re:Seat treats the walking-table minutes as the typical walk and the
# time as lognormal with that spread: sigma = sqrt(ln(1 + 0.193^2)), our derivation, not the source's.

WALK_CV = 0.193
WALK_SIGMA = math.sqrt(math.log(1 + WALK_CV ** 2))


def walk_allowance(minutes: int, confidence: float) -> int:
    """Minutes to allow for a walk whose typical time is `minutes`, so the walk is done in time with
    probability `confidence`. Never less than the typical walk."""
    z = NormalDist().inv_cdf(confidence)
    return max(minutes, math.ceil(minutes * math.exp(z * WALK_SIGMA)))
