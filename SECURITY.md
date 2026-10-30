# Security

## Supported versions

| Version | Supported |
|---|---|
| 0.8.x, the latest on PyPI | Yes |
| Older | No. Upgrade with `pip install -U reseat` |

## Reporting a vulnerability

Report it privately through [GitHub security advisories](https://github.com/hsiddhu2/reseat/security/advisories/new). Do not open a public issue.

Include the version, your operating system, what you did, what happened, and whether a token, a seat or the local store could be affected. You should get a reply within 7 days. A confirmed issue is fixed in a new release, and the advisory is published once the fix is on PyPI.

Out of scope: a flaw in the AWS Events API itself. Report that to AWS.

## Threat model

re:Seat runs on the attendee's own machine. What it protects, and how:

| Asset | Protection |
|---|---|
| Access and refresh tokens | Kept in the OS keychain. Never logged, written to disk or put in a URL. The client attaches the access token only when the base URL is `https://api.awsevents.com` ([ADR-0002](docs/adr/0002-token-host.md)). `reseat logout` asks the sign-in service to revoke the refresh token, then deletes both tokens from the keychain. |
| The local store, `~/.reseat` | Folder 0700, database 0600, opened without following links. It holds the catalog, band history and a journal of writes, no tokens. |
| The rules file | Read without following links. re:Seat warns when others can read a rules file that holds `serve_secret` or `ntfy_topic`. |
| The web app, `reseat serve` | Binds to 127.0.0.1 by default and refuses 0.0.0.0. Any other address needs `serve_secret`, entered once at /login or carried by a one-time link, which sets an HttpOnly, SameSite=Strict cookie. On loopback with no secret there is no sign-in, and the Host header must be loopback. CSP, frame and referrer headers are set. Actions need a matching origin and an `X-Reseat` header ([ADR-0013](docs/adr/0013-web-app.md)). |
| Writes from an AI app or the phone | Neither the web app, the phone nor the MCP server takes raw session ids for a write. Each write approves a plan id re:Seat itself proposed, once, before it expires ([ADR-0014](docs/adr/0014-plan-ids.md)). |
| ntfy pushes | Off unless `ntfy_topic` is set. Pushes carry session codes, titles, status lines, and a link to the web app when it is reachable off loopback, never the access token, an abstract or a session id. With `ntfy_approve: true` the buttons carry a plan id. **The topic is the secret.** Anyone who knows it can derive the reply topic and post a tap. The damage is bounded by the plan id rule above ([ADR-0015](docs/adr/0015-notifications.md)). |
| Email and text | Through the attendee's own Amazon SNS topic and AWS profile. re:Seat stores no AWS keys. |

## Known limits

- The web app is plain HTTP on the address it binds. Off loopback, use Tailscale.
- The MCP approval step is an instruction to the AI app, not a gate the server can enforce on the app itself.
- The command line `reseat swap` and `reseat cancel` take session ids and ask y/N, which `--yes` skips, except that `cancel --yes` is refused while the band is not open.
- The quota window lives in each process ([ADR-0006](docs/adr/0006-quota.md)).
