# The models behind re:Seat

re:Seat acts on a live system that has no swap, no seat counts and no way to tell whether a lost request went through. Each decision is checked against an established result. This page names the result, gives the formula, says how re:Seat uses it, and links the code and the tests. Where a number is a judgement rather than data, it says so.

| | Model | Source | In re:Seat |
|---|---|---|---|
| 1 | The safe swap is a saga | Garcia-Molina and Salem 1987 | [swap.py](../src/reseat/swap.py) |
| 2 | A lost reply means once or not at all | Birrell and Nelson 1984, Akkoyunlu et al. 1975 | [client.py](../src/reseat/client.py), [router.py](../src/reseat/router.py) |
| 3 | Booking order under a quota | Atar, Giat and Shimkin 2010, Cygan et al. 2013 | [router.py](../src/reseat/router.py), tested, kept |
| 4 | Leave in time with a chosen confidence | Fan, Kalaba and Moore 2005, FHWA 2005, Weidmann 1993 | [decisions.py](../src/reseat/decisions.py), [guard.py](../src/reseat/guard.py) |
| 5 | Walk-up odds with an honest range | Gelman et al. 2013, Agresti and Coull 1998 | [decisions.py](../src/reseat/decisions.py), [guard.py](../src/reseat/guard.py) |
| 6 | Checking sessions on site under a read quota | Cho and Garcia-Molina 2003 | [watcher.py](../src/reseat/watcher.py) |

## 1. The safe swap is a saga

**The problem.** To move from a held session A to a better sitting B at the same time, the API needs A cancelled before B can be reserved. There is no atomic swap, and for a moment you hold neither.

**The model.** A saga is a long transaction written as a sequence of steps T1 ... Tn, each with a compensating step Ci that undoes it semantically. The guarantee is that either T1 ... Tn all run, or T1 ... Tj run and then Cj ... C1 (Garcia-Molina and Salem, "Sagas", SIGMOD 1987, pp. 249 to 259, doi:10.1145/38713.38742). The paper's own example is an airline seat reservation.

**In re:Seat.** A swap is a two-step saga: T1 cancels A, T2 reserves B, and C1 reserves A again. A compensation can fail too, if someone takes A's seat in that moment, so re:Seat adds a precondition the paper does not need: before T1, a fresh read must show B open and a way back to A, its own seat or another open sitting of the talk. If the compensation still fails, re:Seat tries each fallback once and reports exactly what is held. Every step is journaled before and after.

Tests: [every read comes before the cancel](../tests/test_swap.py#L71), [B full after the cancel, A recovered](../tests/test_swap.py#L80), [A gone too, fallback booked](../tests/test_swap.py#L93).

## 2. A lost reply means once or not at all

**The problem.** A booking request times out. Did it go through?

**The model.** Birrell and Nelson state the guarantee of a remote call that fails: "the procedure will have been invoked either once or not at all, the user is not told which" ("Implementing Remote Procedure Calls", ACM TOCS 2(1), 1984, pp. 39 to 59, doi:10.1145/2080.357392). Akkoyunlu, Ekanadham and Huber put it as a fact of distributed systems: "In a distributed system with time-outs, it is impossible to provide complete status (even if the system is absolutely reliable)" (SOSP 1975, pp. 67 to 74, doi:10.1145/800213.806523), the origin of the two generals problem.

**In re:Seat.** Reserve and cancel are not idempotent, so a retry after a lost reply can book twice or cancel a seat booked a moment ago. re:Seat never retries a write. After a timeout, a server error or an unreadable answer it reads the schedule back, and the schedule decides.

Tests: [5xx on a booking is read back, not retried](../tests/test_router.py#L262), [the same for a cancel](../tests/test_cancel_and_cli.py#L58), [a timeout on a reserve that landed is verified, not undone](../tests/test_swap.py#L318).

## 3. Booking order under a quota

**The problem.** The API accepts 30 sessions a minute for reserve. When more targets are open than that, which go first?

**The model.** Choosing which of several opportunities to take first, when each may vanish before the next round, is the problem studied by Cygan, Englert, Gupta, Mucha and Sankowski ("Catch them if you can: how to serve impatient users", ITCS 2013, pp. 485 to 494, doi:10.1145/2422436.2422489). No efficient algorithm for the optimal order is known, so index rules are used instead. The classic one ranks by value times rate over abandonment, c times mu over theta (Atar, Giat and Shimkin, Operations Research 58(5), 2010, pp. 1427 to 1439, doi:10.1287/opre.1100.0826).

**In re:Seat.** The order is: formats that fill first, then the attendee's own order. That is an index rule in which fill speed outweighs value. We tested the textbook alternative, the attendee's value (1 for the first target, 1/2 for the second and so on) times a relative fill speed from the format and the seat band, on the fault storm: an hour on the real catalog with 30 percent of reserves full, a 429 every minute, a 503 and 15 minutes of 409. Over ten seeds, the value of the seats held at the end was 3.137 on average with the new order against 3.127 with the current one, better in five seeds and worse in five. That is no real difference, so re:Seat keeps the simpler rule. The storm fills seats at random, so it cannot reward a smarter order. The live catalog will be the real test.

## 4. Leave in time with a chosen confidence

**The problem.** Doors close 11 minutes before a session. The walk between two venues varies with crowds and wayfinding, and a single number is wrong half the time.

**The model.** Fan, Kalaba and Moore frame route choice as maximising the probability of arriving by a deadline ("Arriving on time", JOTA 127(3), 2005, pp. 497 to 513, doi:10.1007/s10957-005-7498-5). For one fixed route this becomes: leave at the deadline minus the alpha-quantile of the travel time. That quantile is the planning time behind the US Federal Highway Administration's buffer index, (95th percentile time minus average time) / average time ("Travel Time Reliability: Making It There On Time, All The Time", 2005). Walking speeds vary around their mean with a standard deviation of 19.3 percent, 0.26 m/s around 1.34 m/s (Weidmann, "Transporttechnik der Fussgänger", Schriftenreihe des IVT 90, ETH Zürich, 2nd ed. 1993, section 5.12.6).

**In re:Seat.** The walking table gives the typical walk m. The time is taken as lognormal with sigma = sqrt(ln(1 + 0.193^2)), about 0.19. That is our derivation from Weidmann's spread, not his figure. The minutes allowed are m times exp(z_alpha times sigma), rounded up, with alpha set by `arrive_confidence` in the rules file, 0.8 by default:

| Typical walk | 50% | 80% (default) | 95% |
|---|---|---|---|
| 10 min | 10 | 12 | 14 |
| 20 min | 20 | 24 | 28 |
| 40 min | 40 | 47 | 55 |
| 60 min | 60 | 71 | 83 |

Leave at = start minus 11 minutes minus the allowance, minus the buffer when the change is tight. A change counts as tight, and a walk as one you cannot make in time, by the typical walk, so the allowance and the buffer do not stack. The table itself is still an estimate, and every screen says so.

Tests: [the allowance is the lognormal quantile](../tests/test_decisions.py), [leave times at 50 percent match the table](../tests/test_guard.py).

## 5. Walk-up odds with an honest range

**The problem.** Is it worth joining the walk-up line? The API gives no room capacity and no line length, only a seat band.

**The model.** Each time a session fills and then frees a seat again counts as a success, each fill that never frees one as a failure. With a Beta(a, b) prior, the posterior after y successes in n fills is Beta(a + y, b + n - y), with mean (a + y) / (a + b + n) (Gelman et al., "Bayesian Data Analysis", 3rd ed., 2013, section 2.4). Agresti and Coull show that adding pseudo-observations gives honest intervals from few observations (The American Statistician 52(2), 1998, pp. 119 to 126, doi:10.1080/00031305.1998.10480550).

**In re:Seat.** The prior is the rule for the session type, worth 4 observations: breakouts and lightning talks 0.70, chalk and code talks 0.45, workshops, labs, bootcamps and builders' sessions 0.15, and 0.5 for other types. These priors are judgements. Only fills re:Seat sees happen count: a session already full the first time re:Seat looks is no evidence either way, and a band that turns walk-up only frees no reserved seat. Each fill updates the odds smoothly, where the old rule switched at three band changes. The page shows the mean and an 80 percent range, for example a chalk talk seen to fill twice and free a seat once reads "walk-up about 47% (23 to 71%)". The range is wide until re:Seat has seen the session fill a few times, and that is the honest answer. The outcome is a proxy: re:Seat sees seats free up, not whether a person got in.

Tests: [the posterior and its interval](../tests/test_decisions.py), [history moves the odds gradually](../tests/test_guard.py).

## 6. Checking sessions on site under a read quota

**The problem.** During the event re:Seat checks the day's sessions one by one between catalog sweeps, inside the 120 a minute GetSession quota.

**The model.** Cho and Garcia-Molina study how to split a fixed refresh budget across items that change at different rates. Their result is that "the uniform policy is always more effective than the proportional policy", and that the optimal policy penalises items that change too often ("Effective page refresh policies for Web crawlers", ACM TODS 28(4), 2003, pp. 390 to 426, doi:10.1145/958942.958945).

**In re:Seat.** On site it checks the day's held and wanted sessions uniformly, up to 40 of them every 20 seconds, which is the policy that result favours over checking the busiest sessions most. The optimal allocation needs each session's change rate, which only the live event can provide.

## What these models are not

They are not predictions. The API has no seat counts, no capacity and no waitlist, so every probability here comes from the seat band, the session type, the attendee's own order and what re:Seat saw in its own sweeps. Judgements are named in the code ([decisions.py](../src/reseat/decisions.py)) and on this page.
