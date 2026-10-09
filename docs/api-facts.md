# AWS Events API, verified facts

What re:Seat relies on about the AWS Events API. Every fact was read from the OpenAPI spec or an AWS developer guide page on 29 or 30 September 2026, or observed in the live catalog where marked. The source is beside each section. If [`openapi.json`](openapi.json) disagrees with this file, the spec wins.

## Endpoints

Source: https://docs.aws.amazon.com/events/latest/devguide/rest-api.html and https://api.awsevents.com/v1/openapi.json

- Base URL `https://api.awsevents.com`. Every path begins with `/v1`. JSON in and out.
- MCP server at `https://api.awsevents.com/mcp` over streamable HTTP. Same twelve operations as tools. The MCP server requires sign-in for every call, even catalog reads.

| Operation | Method and path | Auth |
|---|---|---|
| ListEvents | GET /v1/events?includePast= | none |
| GetEvent | GET /v1/events/{eventId} | none |
| ListSessions | GET /v1/events/{eventId}/sessions?locale=&includeAbstracts=&nextToken= | attendee, for events that require registration |
| GetSession | GET /v1/events/{eventId}/sessions/{sessionId}?locale= | same |
| GetSchedule | GET /v1/events/{eventId}/schedule | attendee |
| ReserveSessions | POST /v1/events/{eventId}/reservations  body {sessionIds:[1..10]} | attendee |
| CancelReservation | DELETE /v1/events/{eventId}/reservations/{sessionId} | attendee |
| AssociateFavorites | POST /v1/events/{eventId}/favorites  body {sessionIds:[1..10]} | attendee |
| DisassociateFavorite | DELETE /v1/events/{eventId}/favorites/{sessionId} | attendee |
| CreatePersonalTime | POST /v1/events/{eventId}/personal-time | attendee |
| UpdatePersonalTime | PUT /v1/events/{eventId}/personal-time/{personalTimeId} | attendee |
| DeletePersonalTime | DELETE /v1/events/{eventId}/personal-time/{personalTimeId} | attendee |

re:Invent 2026 event id is `reinvent2026`. It requires registration, so the catalog is not readable anonymously.

## Behaviour that matters

Source: operation descriptions in the spec.

- ListSessions: pages of up to 250, ordered by sessionId, opaque `nextToken`, absent on the last page. `totalCount` is the catalog size. `includeAbstracts=false` omits only the abstract. `locale` is a BCP-47 tag, falls back to en-US, and the response carries `Content-Language`.
- The API does not search or filter. Pull everything and filter locally.
- ReserveSessions and AssociateFavorites return 200 with a `result` of `successful` and `failed`. "A 200 does not mean everything was added; always read `failed`." A session already held is reported in `failed`, "so re-sending a request is not a safe retry."
- CancelReservation on a session not held is 404, "so this is not a safe blind retry." Same for DisassociateFavorite.
- DeletePersonalTime on an entry already gone succeeds. "A retry is safe." It is the only idempotent write.
- UpdatePersonalTime is a full replacement. Every field is required and an omitted optional field is cleared.
- Reserving a session that overlaps a held one fails per session with `scheduleConflict` and `conflictsWith`, the list of the caller's held sessions that overlap. There is no swap or replace operation.

## Failure codes on bulk results

Source: `BulkFailureCode` in the spec. "Treat an unrecognized value as a generic refusal: values are added as the event platform grows."

`sessionNotReservable`, `scheduleConflict`, `alreadyScheduled`, `sessionFull`, `insufficientAccess`, `timePassed`, `alreadyFavorited`, `notFavorited`, `other`.

## HTTP errors

Source: spec response schemas.

| Status | Meaning | What to do |
|---|---|---|
| 400 | ValidationException. Not retriable. | Fix the request. |
| 401 | Not signed in, or token expired. | Refresh once, retry once. Second 401 means sign in again. |
| 403 | Signed in but not registered for this event. | Stop. Tell the attendee. |
| 404 | Event, session or personal time entry not found. | For cancel and unfavorite this means not held. |
| 409 | OperationUnavailableException. "Intentionally disabled. Retrying will not help until it is re-enabled." | Surface it. Do not retry. |
| 429 | ThrottlingException with `Retry-After` in seconds. | Wait that long once, then retry. |
| 500 | Internal error. | Surface it. |
| 503 | Temporarily unavailable. "Clients should back off and retry." | Back off, retry a bounded number of times. |

## Quotas

Source: https://docs.aws.amazon.com/events/latest/devguide/quotas.md

Per attendee, per minute: GetSession 120, ListSessions 120, GetSchedule 60, ReserveSessions 30 sessions, CancelReservation 30, AssociateFavorites 30 sessions, DisassociateFavorite 30, CreatePersonalTime 30, UpdatePersonalTime 30, DeletePersonalTime 30.

"ReserveSessions and AssociateFavorites count each session named in the request, not each request. A batch is no cheaper than one request per session. It only saves round trips."

"When you exceed a quota, the API returns 429 with a Retry-After header giving the seconds to wait, which is the time left in the current minute before the quota resets. If a batch is larger than the quota you have left, make it smaller and retry right away, because a refused request spends none of the quota."

## Dates

Source: https://docs.aws.amazon.com/events/latest/devguide/what-is-events-api.html

"Reserved seating for AWS re:Invent opens on October 6, 2026, and opens through this API on October 8, 2026. Until then, reserving and canceling return 409. Reading the catalog and marking favorites work as normal."

re:Invent runs 30 November to 4 December 2026.

## Session fields

Source: `Session` schema in the spec. Only `sessionId` and `title` are required.

`sessionId`, `title`, `abbreviation` (public code such as AIM3315, repeats carry -R1, -R2), `abstract`, `type` (e.g. Chalk talk, Workshop), `level` (e.g. 300 - Advanced), `venue`, `room`, `isAllDaySession`, `isReservable`, `seatAvailability`, `sessionTime` {date, time, length in minutes as a string, timezone}, `speakers` [{name}], `tracks`, `topics`, `industries`, `areasOfInterest`, `roles`, `services`, `segments`, `features`, `customerPersonas`, `experiences` (named programs, attendance may be restricted), `additionalActivities`, `focusAreas`.

`seatAvailability` is a band, present only on reservable sessions: `available`, `limited`, `veryLimited`, `unavailable`, `walkUp`. "walkUp means no reservation is taken; attend by walking up." There is no seat count and no waitlist in this API.

Not in the API: speaker IDs or bios, recording status, room capacity, other attendees, accessibility flags.

## Observed in the live catalog, 1 October 2026

Source: `reseat sync` and `reseat save-fixture` run against reinvent2026 on 1 October 2026 by a registered attendee. Fixture `tests/fixtures/catalog-2026-10-01.json`. These are observations, not spec guarantees.

- 2068 sessions, 9 pages of 250.
- `venue` is one of `MGM Grand`, `Caesars Forum`, `Venetian`, or absent. It is absent on 909 sessions. Then the venue is the first segment of `room`: `Caesars Palace` or `Wynn/Encore`. AWS names Wynn and Encore as one venue.
- `room` is segments joined by ` | `, for example `Level 1 | Forum 120 | Content Hub | Blue Theater`.
- 62 sessions have no `sessionTime` and no `room`.
- `sessionTime.time` is `HH:MM`. `sessionTime.timezone` is never sent.
- `type` values and counts: Chalk talk 640, Breakout session 485, Workshop 291, Builders' session 243 (straight apostrophe), Lightning talk 213, Code talk 119, Lab 31, Gamified learning 26, Bootcamp 16, Exam prep 4.
- `isReservable` is false on every session and `seatAvailability` is never sent. Reserved seating opens 6 October.
- Codes: 485 end in `-R`, 445 in `-R<n>`, 1138 have no suffix. 1625 base codes, 434 with more than one sitting.
- Empty lists are omitted, not sent as `[]`.
- The `id` in a reinvent-planner.cloud export is the Events API `sessionId`. Checked on 2 October 2026: both sittings of COP324 from the sample export resolve in the local catalog by id.
- The spec defines `isReservable` as "Whether the session accepts seat reservations." re:Seat never plans a session where it is false. On 1 October it was false everywhere, so every plan was empty. It is expected to change when reserved seating opens on 6 October. `reseat book --dry-run` shows the current state.

## Observed in the live API, 8 October 2026

Source: [proof/2026-10-08-catalog-empty.md](proof/2026-10-08-catalog-empty.md). Observations, not spec guarantees.

- On the evening of 8 October, the day writes were scheduled to open through the API, ListSessions answered 200 with `totalCount` 0 and no items, and GetSession answered 404 for every session tried, including held ones. GetSchedule, GetEvent and ListEvents still worked.
- Reservations made after seating opened carry session ids in a range absent from the 1 October catalog, which suggests the catalog was re-keyed.
- re:Seat therefore refuses a sweep that comes back empty or loses most of the catalog without bringing back one of about the same size, checks each walk against `totalCount`, records a re-keyed catalog as a new baseline rather than as news, and books nothing while a held session is missing from its local catalog.

## Schedule fields

Source: `Schedule` and `PersonalTime` schemas.

`reserved` and `favorites` are lists of session IDs. "Look a session up to get its details." `personalTime` is a list of {personalTimeId, startDateTime, endDateTime, title, description, location}.

## Personal time input

Source: `PersonalTimeInput` schema.

- `startDateTime` and `endDateTime`: `YYYY-MM-DDTHH:MM:SS`. "Always UTC, so a trailing Z or an offset such as +01:00 is rejected rather than converted. A block is stored to the minute, so the seconds must be 00."
- End after start. "The length between the two must be a whole number of 5-minute increments."
- `title` 1 to 128 chars, `description` 1 to 250 chars, both required. `location` 1 to 255, optional.

## Authentication

Source: https://docs.aws.amazon.com/events/latest/devguide/auth-signing-in.md and neighbouring pages.

- OAuth 2.0 authorization code flow with PKCE. Client id `7vmom55m1qstvq8i71ph127bfq`, shared by all callers. Scope `openid email events/access`. `identity_provider=AWSBuilderID`.
- Authorize at `https://oauth.awsevents.com/oauth2/authorize`. Token at `https://oauth.awsevents.com/oauth2/token`. Revoke at `https://oauth.awsevents.com/oauth2/revoke`.
- "Your application must run on the attendee's machine. It listens at /callback on one of six reserved loopback ports, 8484 through 8489." Both `localhost` and `127.0.0.1` are registered. `redirect_uri` is matched exactly. "There is no hosted redirect URI."
- Send `code_challenge_method=S256` explicitly. Challenge is base64url without padding. Verifier fresh per attempt, 43 to 128 chars.
- Access token is a JWT valid 60 minutes. Refresh token is opaque, valid 30 days. ID token is not accepted by the API.
- Refresh: POST token endpoint with `grant_type=refresh_token`. "Schedule the next refresh against the response's expires_in. If the response carries a new refresh token, replace the one you stored."
- "Treat a 401 as refresh once, then retry the request. If the retry also returns 401, the refresh token is no longer good."
- Sign-out: revoke the refresh token, delete both tokens. Ending the Builder ID browser session needs a redirect chain a CLI cannot drive. Point the attendee to `https://profile.aws.amazon.com`.
- Token handling: "Do not log tokens, put them in URLs, or store them where other code on the device can read them." Use the OS keychain.
