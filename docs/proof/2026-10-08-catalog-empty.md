# The catalog came back empty on the day writes were scheduled to open

- **Date and time:** 2026-10-08 20:19 to 20:28 PDT. The catalog was still empty at 20:28.
- **re:Seat version:** `132c972`
- **Where:** the attendee's laptop, signed in with the Builder ID registered for re:Invent 2026

## What was checked

The planned band inspection for after reserved seating opened on 6 October: sync the catalog and look at the seat bands. The API returned no catalog at all.

## Command

```
reseat sync --no-abstracts
```

## Output

```
Synced 0 sessions in 1.4s. Added 0, removed 2068, moved 0, band changes 0, newly
open 0.
```

re:Seat read the empty answer as "all 2,068 sessions removed" and emptied its local copy of the catalog. Nothing on the attendee's AWS schedule changed: re:Seat sent no write.

## Follow-up reads

Read-only calls through re:Seat's client, to see what the API was serving:

```
ListSessions                    -> 200, totalCount 0, items 0, no nextToken
ListSessions locale=en-US       -> 200, totalCount 0, items 0
GetSession <held session>       -> 404 "No session was found with the requested id"
GetSession <held session>       -> 404 "No session was found with the requested id"
GetSession <a 1 October id>     -> 404 "No session was found with the requested id"
GetSchedule                     -> 200, reserved 14, favorites 16, personal time 0
GetEvent reinvent2026           -> 200, 2026-11-30 to 2026-12-04, authentication required
ListEvents                      -> reinvent2026 listed with two other events
```

The 14 reservations were made in the AWS portal after seating opened on 6 October. Their session ids begin `17842318`. No id in the 1 October catalog begins that way: those begin `1780` and `1790`.

## Result

On the evening of 8 October the Events API answered GetSchedule but served no sessions: an empty catalog and 404 for every session tried, including held ones.

The same condition was reported publicly on 3 October by another re:Invent tool, in [reinvent-scout issue 17](https://github.com/jasonwadsworth/reinvent-scout/issues/17): `totalCount` 0, GetSession 404 for every id in its user's favorites, GetSchedule still returning the user's favorites, and the official MCP server returning the same empty list.

Three re:Seat fixes came from this, each tested against the fake:

- A sweep that comes back empty, or would drop more than half the catalog without bringing back a catalog of about the same size, is refused. The local catalog is kept and the watcher reports the API as unusable, while on-site polling of single sessions carries on.
- A sweep of about the same size whose ids are mostly new is recorded as a new baseline. New ids are not reported as new sessions, so the watcher does not act on a re-keyed catalog as if it were 2,000 new sessions. A session that kept its id can still open.
- While any held session is missing from the local catalog, nothing is booked: re:Seat cannot tell which talks those are, and could otherwise book a second sitting of one. The 14 held sessions here are in exactly that state until the catalog returns.

## What it does not prove

- Why the catalog was empty, or for how long. It may have been a short outage on the API side.
- Whether session ids changed. A sync once the catalog is back will show that.
- Anything about booking through the API. No write was sent.

## Daily checks

One line per check until the catalog returns, as `date time, command, result`. The first sync that returns sessions is the band inspection and gets its own file.

```
2026-10-08 20:19 PDT  reseat sync --no-abstracts  ListSessions 200, totalCount 0 (the run above)
2026-10-08 20:28 PDT  ListSessions read            200, totalCount 0
2026-10-08 21:07 PDT  reseat sync --no-abstracts  refused: the API returned an empty catalog
```
