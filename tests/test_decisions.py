"""The models behind re:Seat's decisions, checked against their formulas."""

import math

import pytest

from reseat import decisions as D


def test_beta_binomial_posterior_mean_and_interval():
    # Prior 0.45 with weight 4 is Beta(1.8, 2.2). Two fills, one freed: Beta(2.8, 3.2).
    h = [(0, None, "limited"), (1, "limited", "unavailable"), (2, "unavailable", "available"),
         (3, "available", "unavailable")]
    o = D.walk_up_odds(0.45, h)
    assert (o.fills, o.freed) == (2, 1)
    assert o.p == pytest.approx(2.8 / 6.0)
    half = D.Z80 * math.sqrt(o.p * (1 - o.p) / 7.0)
    assert (o.lo, o.hi) == (pytest.approx(o.p - half), pytest.approx(o.p + half))


def test_more_observations_narrow_the_interval():
    few = D.walk_up_odds(0.45, [])
    seen = [(0.0, None, "available")] + [
        (float(i), "available" if i % 2 else "unavailable", "unavailable" if i % 2 else "available")
        for i in range(1, 41)]
    lots = D.walk_up_odds(0.45, seen)
    assert (lots.hi - lots.lo) < (few.hi - few.lo)


def test_a_flapping_band_counts_each_fill_once():
    h = [(0, None, "limited"), (1, "limited", "unavailable"), (2, "unavailable", "unavailable"),
         (3, "unavailable", "limited"), (4, "limited", "limited")]
    assert D.fill_episodes(h) == (1, 1)


def test_unknown_type_with_nothing_seen_says_unknown():
    assert D.verdict(D.walk_up_odds(None, []), known=False) == "unknown"
    filled = [(0, None, "available"), (1, "available", "unavailable")]
    assert D.verdict(D.walk_up_odds(None, filled), known=False) == "early"   # 2/5


@pytest.mark.parametrize("minutes", [10, 20, 40, 60])
def test_walk_allowance_is_the_lognormal_quantile(minutes):
    assert D.walk_allowance(minutes, 0.5) == minutes
    z = 0.8416212335729143
    assert D.walk_allowance(minutes, 0.8) == math.ceil(minutes * math.exp(z * D.WALK_SIGMA))
    assert D.walk_allowance(minutes, 0.95) > D.walk_allowance(minutes, 0.8) > minutes


def test_walk_sigma_comes_from_a_19_percent_spread():
    assert D.WALK_SIGMA == pytest.approx(math.sqrt(math.log(1 + 0.193 ** 2)))
    assert 0.18 < D.WALK_SIGMA < 0.2


# ---- QA: walk-up odds edge cases


def test_empty_history_returns_the_prior_with_no_fills():
    o = D.walk_up_odds(0.7, [])
    assert (o.fills, o.freed) == (0, 0) and o.p == pytest.approx(0.7)
    assert D.verdict(o, known=True) == "likely"


def test_history_that_never_fills_does_not_move_the_odds():
    h = [(1.0, None, "available"), (2.0, "available", "limited"), (3.0, "limited", "veryLimited")]
    o = D.walk_up_odds(0.45, h)
    assert (o.fills, o.freed) == (0, 0) and o.p == pytest.approx(0.45)
    assert D.verdict(D.walk_up_odds(None, h), known=False) == "unknown"


def test_a_session_already_full_when_first_seen_is_not_a_fill():
    """re:Seat did not see it fill, so it is no evidence about walk-ups (Engineer review)."""
    assert D.fill_episodes([(1.0, None, "unavailable")]) == (0, 0)
    assert D.fill_episodes([(1.0, None, "unavailable"), (2.0, "unavailable", "available"),
                            (3.0, "available", "unavailable")]) == (1, 0)


def test_walk_up_only_is_not_a_freed_seat():
    h = [(0.0, None, "limited"), (1.0, "limited", "unavailable"), (2.0, "unavailable", "walkUp")]
    assert D.fill_episodes(h) == (1, 0)


def test_history_is_read_in_time_order():
    h = [(2.0, "unavailable", "available"), (1.0, "limited", "unavailable"), (0.0, None, "limited")]
    assert D.fill_episodes(h) == (1, 1)


def test_many_fills_all_freed_is_likely_even_for_a_workshop():
    h = [(0.0, None, "available")] + [
        (float(i), "available" if i % 2 else "unavailable", "unavailable" if i % 2 else "available")
        for i in range(1, 21)]
    o = D.walk_up_odds(D.PRIOR_ODDS["unlikely"], h)
    assert (o.fills, o.freed) == (10, 10) and D.verdict(o, known=True) == "likely"


@pytest.mark.parametrize("prior", [None, 0.15, 0.45, 0.7])
@pytest.mark.parametrize("fills,freed", [(0, 0), (1, 0), (1, 1), (30, 0), (30, 30)])
def test_interval_stays_within_zero_and_one(prior, fills, freed):
    h = []
    for i in range(fills):
        h.append((2.0 * i, None, "unavailable"))
        h.append((2.0 * i + 1, None, "available" if i < freed else "unavailable"))
    o = D.walk_up_odds(prior, h)
    assert 0.0 <= o.lo <= o.p <= o.hi <= 1.0


def test_a_weak_prior_is_clipped_at_zero_and_percent_reads_the_range():
    o = D.walk_up_odds(0.15, [])
    assert o.lo == 0.0
    assert D.percent(o) == f"about 15% (0 to {round(o.hi * 100)}%)"
