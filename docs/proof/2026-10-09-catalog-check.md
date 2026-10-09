# The catalog re-checked through three paths

- **Date and time:** 2026-10-09 10:14 to 10:15 PDT
- **re:Seat version:** `b07e1e0`
- **Where:** the attendee's laptop, signed in with the Builder ID registered for re:Invent 2026

## What was checked

Whether the Events API serves the re:Invent 2026 catalog, checked fresh rather than read from the 8 October record. The same question was asked three ways: through re:Seat, through a direct HTTPS request that bypasses re:Seat's client, and through the official Events API MCP server.

## Commands and output

### 1. `reseat events`

```
┏━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━┓
┃ ID                   ┃ Name                 ┃ Start      ┃ End        ┃ Auth ┃
┡━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━┩
│ Cohort-CloudRiyadh-… │ AWS Cloud and AI Day │ 2026-10-21 │ 2026-10-21 │ no   │
│                      │ Riyadh 2026          │            │            │      │
│ reinvent2026         │ re:Invent 2026       │ 2026-11-30 │ 2026-12-04 │ yes  │
│ Cohort-CloudKualaLu… │ AWS Cloud and AI Day │ 2026-11-04 │ 2026-11-04 │ no   │
│                      │ Kuala Lumpur 2026    │            │            │      │
└──────────────────────┴──────────────────────┴────────────┴────────────┴──────┘
exit=0
```

### 2. `reseat whoami`

```
Registered for reinvent2026. 14 reserved, 16 favorites, 0 personal time entries.
exit=0
```

### 3. ListSessions over plain HTTPS

A direct GET, outside re:Seat's client and its parsing. The access token came from the keychain into memory and was sent only in the request header. It was not printed or passed on a command line.

```
$ GET https://api.awsevents.com/v1/events/reinvent2026/sessions?includeAbstracts=false
HTTP 200 OK
content-type: application/json
{"items":[],"totalCount":0}
```

### 4. ListSessions through the official MCP server

JSON-RPC over streamable HTTP to `https://api.awsevents.com/mcp`, signed in the same way.

```
$ MCP initialize https://api.awsevents.com/mcp
HTTP 200 | server: {'version': '1.0.0', 'name': 'AWSEventsPublicApi-Mcp-prod'}
$ MCP tools/list -> HTTP 200 | 12 tools
ListSessions input properties: ['eventId', 'includeAbstracts', 'nextToken', 'locale']
$ MCP tools/call AWSEventsPublicApi-Mcp-prod___ListSessions {"eventId": "reinvent2026", "includeAbstracts": false}
HTTP 200
{"isError": false, "content": [{"type": "text", "text": "{\"items\":[],\"totalCount\":0}"}]}
```

### 5. `reseat sync --no-abstracts`

```
The API returned an empty catalog. The 0 sessions stored locally were kept. Run 
reseat sync --force if the catalog really changed that much. Otherwise run again
later.
exit=4
```

## Result

| Path | HTTP status | totalCount | First session id |
|---|---|---|---|
| re:Seat sync | 200, refused locally | 0 | none |
| Direct HTTPS | 200 | 0 | none |
| Official MCP server | 200 | 0 | none |

On 9 October at 10:14 PDT the Events API answered ListEvents and GetSchedule, and served an empty re:Invent 2026 catalog through both the REST API and its own MCP server. re:Seat's view matches what the API serves. Nothing was written.

## What it does not prove

- Why the catalog is empty, or when it will return.
- Anything about booking through the API. No write was sent.
