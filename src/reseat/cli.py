"""re:Seat command line.

Week one commands. Reads are live today. Writes to reservations open 8 October.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import auth, config, fixtures
from . import cancel as cancel_engine
from . import rules as rules_mod
from .client import ApiError, EventsClient, NotRegistered, OperationClosed
from .router import OP as RESERVE_OP
from .router import WRITES_CLOSED, Execution, Plan, Router, run_booking
from .store import Store

app = typer.Typer(help="re:Seat keeps your re:Invent seats.", no_args_is_help=True)
rules_app = typer.Typer(help="Create, import and check your rules file.", no_args_is_help=True)
app.add_typer(rules_app, name="rules")
con = Console()


def _client() -> EventsClient:
    store = auth.TokenStore()

    def refresh() -> str:
        t = store.load()
        if not t:
            raise auth.AuthError("Not signed in. Run: reseat login")
        return auth.refresh(t, store).access_token

    return EventsClient(token_provider=lambda: auth.current_access_token(store), on_refresh=refresh)


def _store() -> Store:
    config.ensure_home()
    return Store(config.DB_PATH)


# --------------------------------------------------------------------------- auth


@app.command()
def login(no_browser: bool = typer.Option(False, help="Print the URL instead of opening a browser.")):
    """Sign in with your AWS Builder ID."""
    try:
        auth.login(open_browser=not no_browser)
    except auth.AuthError as e:
        con.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    con.print("[green]Signed in.[/green] Tokens are in your OS keychain.")


@app.command()
def logout():
    """Revoke the refresh token and forget both tokens."""
    auth.logout()
    con.print("Signed out of re:Seat. To end the Builder ID browser session too, "
              "visit https://profile.aws.amazon.com")


@app.command()
def whoami(event: str = config.DEFAULT_EVENT):
    """Check sign-in and registration for the event."""
    c = _client()
    try:
        s = c.get_schedule(event)
    except NotRegistered:
        con.print(f"[red]Signed in, but this Builder ID is not registered for {event}.[/red]")
        raise typer.Exit(2) from None
    except ApiError as e:
        con.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    con.print(f"[green]Registered for {event}.[/green] "
              f"{len(s.reserved)} reserved, {len(s.favorites)} favorites, "
              f"{len(s.personal_time)} personal time entries.")


# --------------------------------------------------------------------------- catalog


@app.command()
def events(past: bool = typer.Option(False, help="Include events that already ended.")):
    """List AWS events. Needs no sign-in."""
    c = _client()
    t = Table("ID", "Name", "Start", "End", "Auth")
    for e in c.list_events(include_past=past):
        t.add_row(e.event_id, e.name, e.start_date[:10], e.end_date[:10],
                  "yes" if e.authentication_required else "no")
    con.print(t)


@app.command()
def sync(event: str = config.DEFAULT_EVENT,
         abstracts: bool = typer.Option(True, help="Fetch abstracts. Off for a cheap sweep.")):
    """Pull the whole catalog into the local store and report what changed."""
    c, st = _client(), _store()
    t0 = time.time()
    sessions = list(c.iter_sessions(event, include_abstracts=abstracts))
    res = st.apply_sweep(event, sessions, with_abstracts=abstracts)
    con.print(f"Synced {res.count} sessions in {time.time() - t0:.1f}s. "
              f"Added {len(res.added)}, removed {len(res.removed)}, moved {len(res.moved)}, "
              f"band changes {len(res.band_changes)}, newly open {len(res.opened)}.")
    for sid in res.added[:20]:
        s = st.get(event, sid)
        if s:
            con.print(f"  [green]new[/green] {s.abbreviation or ''} {s.title}")
    for sid in res.opened[:20]:
        s = st.get(event, sid)
        if s:
            con.print(f"  [cyan]opened[/cyan] {s.abbreviation or ''} {s.title} -> {s.seat_availability}")


@app.command()
def search(text: str, event: str = config.DEFAULT_EVENT, limit: int = 30):
    """Search the local catalog by title, code or abstract."""
    st = _store()
    t = Table("Code", "Title", "Type", "When", "Venue", "Seats")
    for s in st.search(event, text, limit):
        st_ = s.session_time
        when = f"{st_.date} {st_.time}" if st_ and st_.date else ""
        t.add_row(s.abbreviation or "", s.title[:60], s.type or "", when, s.campus_venue or "",
                  s.seat_availability or "")
    con.print(t)


@app.command()
def show(session_id: str, event: str = config.DEFAULT_EVENT):
    """Show one session from the local catalog, with its repeats and band history."""
    st = _store()
    s = st.get(event, session_id)
    if not s:
        con.print("Not in the local catalog. Run: reseat sync")
        raise typer.Exit(1)
    con.print(f"[bold]{s.abbreviation}  {s.title}[/bold]")
    con.print(f"{s.type} | {s.level} | {s.campus_venue} | {s.room_label}")
    if s.session_time:
        con.print(f"{s.session_time.date} {s.session_time.time} for {s.session_time.length} min")
    con.print(f"Reservable: {s.is_reservable}  Seats: {s.seat_availability}")
    if s.base_code:
        reps = [r for r in st.by_base_code(event, s.base_code) if r.session_id != s.session_id]
        if reps:
            con.print("Repeats:")
            for r in reps:
                con.print(f"  {r.abbreviation} {r.session_time.date if r.session_time else ''} "
                          f"{r.session_time.time if r.session_time else ''} {r.campus_venue} "
                          f"[{r.seat_availability}]  id={r.session_id}")
    hist = st.band_history(event, session_id)
    if hist:
        con.print("Band history:")
        for ts, old, new in hist[-10:]:
            when = datetime.fromtimestamp(ts, UTC).strftime("%m-%d %H:%M")
            con.print(f"  {when}  {old or '-'} -> {new or '-'}")
    if s.abstract:
        con.print(f"\n{s.abstract}")


@app.command("save-fixture")
def save_fixture(event: str = config.DEFAULT_EVENT,
                 out: str = typer.Option("", help="Path. Default tests/fixtures/catalog-<today>.json.")):
    """Save a catalog pull for tests, without abstracts or speaker names."""
    path = Path(out) if out else fixtures.default_path(Path.cwd())
    data = fixtures.pull_catalog(_client(), event)
    fixtures.save_catalog(data, path)
    con.print(f"Saved {data['totalCount']} sessions to {path}")


# --------------------------------------------------------------------------- schedule


@app.command()
def schedule(event: str = config.DEFAULT_EVENT):
    """Show your reserved sessions, favorites and personal time."""
    c, st = _client(), _store()
    s = c.get_schedule(event)

    def row(sid: str) -> list[str]:
        x = st.get(event, sid)
        if not x:
            return [sid, "(not in local catalog, run sync)", "", "", ""]
        t = x.session_time
        return [x.abbreviation or sid, x.title[:55], f"{t.date} {t.time}" if t and t.date else "",
                x.campus_venue or "", x.seat_availability or ""]

    for label, ids in (("Reserved", s.reserved), ("Favorites", s.favorites)):
        t = Table("Code", "Title", "When", "Venue", "Seats", title=f"{label} ({len(ids)})")
        for sid in ids:
            t.add_row(*row(sid))
        con.print(t)
    t = Table("ID", "Start UTC", "End UTC", "Title", "Location", title="Personal time")
    for p in s.personal_time:
        t.add_row(p.personal_time_id, p.start_date_time, p.end_date_time, p.title, p.location or "")
    con.print(t)


@app.command()
def favorite(session_ids: list[str], event: str = config.DEFAULT_EVENT,
             dry_run: bool = typer.Option(False, help="Show what would be sent.")):
    """Mark sessions as favorites. Batches of 10, quota aware."""
    c, st = _client(), _store()
    ids = list(dict.fromkeys(session_ids))
    for i in range(0, len(ids), 10):
        batch = ids[i:i + 10]
        if dry_run:
            con.print(f"would favorite {batch}")
            continue
        try:
            res = c.favorite(event, batch)
        except OperationClosed:
            st.journal(event, "AssociateFavorites", batch, {"status": 409}, "closed")
            con.print("[yellow]Favorites are closed (409). Nothing was changed.[/yellow]")
            raise typer.Exit(3) from None
        outcome = "partial" if res.failed else "success"
        st.journal(event, "AssociateFavorites", batch, res.model_dump(), outcome)
        for sid in res.successful:
            con.print(f"[green]favorited[/green] {sid}")
        for f in res.failed:
            con.print(f"[yellow]{f.session_id}: {f.code}[/yellow]")
    if not dry_run:
        back = c.get_schedule(event)
        missing = [i for i in ids if i not in back.favorites]
        st.journal(event, "GetSchedule", ids, back.model_dump(by_alias=True),
                   "readback-missing" if missing else "readback-ok")
        if missing:
            con.print(f"[red]Read-back does not list as favorites: {', '.join(missing)}[/red]")


@app.command()
def probe(event: str = config.DEFAULT_EVENT,
          session_id: str = typer.Option(..., help="A session you will never hold, e.g. a keynote.")):
    """Check whether reservation writes are open. Never changes your schedule."""
    c = _client()
    if session_id in c.get_schedule(event).reserved:
        con.print("[red]You hold that session. Probing would cancel it. Pick one you can never hold, "
                  "such as a keynote.[/red]")
        raise typer.Exit(2)
    try:
        opened = c.writes_open(event, session_id)
    except OperationClosed:
        opened = False
    con.print("[green]Writes are open.[/green]" if opened else "[yellow]Writes are closed (409).[/yellow]")



# --------------------------------------------------------------------------- rules


def _rules(path: Path | None = None) -> rules_mod.Rules:
    try:
        return rules_mod.load(path or config.RULES_PATH)
    except rules_mod.RulesError as e:
        con.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from None


@rules_app.command("init")
def rules_init(force: bool = typer.Option(False, help="Overwrite an existing rules file.")):
    """Write a commented example rules file."""
    config.ensure_home()
    if config.RULES_PATH.exists() and not force:
        con.print(f"{config.RULES_PATH} exists. Use --force to overwrite it.")
        raise typer.Exit(1)
    config.RULES_PATH.write_text(rules_mod.EXAMPLE, encoding="utf-8")
    con.print(f"Wrote {config.RULES_PATH}. Edit it, then run: reseat rules check")


@rules_app.command("import")
def rules_import(file: Path,
                 force: bool = typer.Option(False, help="Replace targets already in the rules file.")):
    """Import targets from a reinvent-planner.cloud JSON export, in its order."""
    try:
        new = rules_mod.from_planner_export(rules_mod.read_planner_file(file))
    except rules_mod.RulesError as e:
        con.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from None
    config.ensure_home()
    if config.RULES_PATH.exists():
        old = _rules()
        if old.targets and not force:
            con.print(f"{config.RULES_PATH} already has {len(old.targets)} targets. "
                      "Use --force to replace them. Meals and limits are kept.")
            raise typer.Exit(1)
        try:
            new = rules_mod.Rules(**{**old.model_dump(), "targets": [t.model_dump() for t in new.targets]})
        except ValueError as e:
            con.print(f"[red]{e}[/red]")
            raise typer.Exit(2) from None
    rules_mod.save(new, config.RULES_PATH)
    con.print(f"Imported {len(new.targets)} targets into {config.RULES_PATH}.")


@rules_app.command("check")
def rules_check(event: str = config.DEFAULT_EVENT):
    """Validate the rules file against the local catalog and list unresolved codes."""
    r, st = _rules(), _store()
    con.print(f"{len(r.targets)} targets (cap {r.watch_cap}), {len(r.meals)} meals, "
              f"max {r.max_per_day} per day, buffer {r.buffer_minutes} min.")
    missing = rules_mod.unresolved(r, st, event)
    for m in missing:
        con.print(f"  [yellow]unresolved[/yellow] {m}")
    if missing:
        con.print("Run reseat sync if the catalog is old, then fix the codes above.")
        raise typer.Exit(1)
    con.print("[green]All targets resolve in the local catalog.[/green]")


# --------------------------------------------------------------------------- booking


def _print_plan(plan: Plan, store: Store, event: str) -> None:
    t = Table("#", "Code", "Type", "When", "Venue", "Target", "Kind", title="Plan, sent in this order")
    for i, p in enumerate(plan.batch, 1):
        s = store.get(event, p.session_id)
        when = f"{s.session_time.date} {s.session_time.time}" if s and s.session_time else ""
        t.add_row(str(i), p.code, p.type or "", when, (s.campus_venue if s else "") or "",
                  p.target, "backup" if p.backup else "sitting")
    con.print(t)
    if plan.deferred:
        con.print(f"Over this minute's quota, next round: {', '.join(p.code for p in plan.deferred)}")
    if plan.held_targets:
        con.print(f"Already held: {', '.join(plan.held_targets)}")
    if plan.exhausted:
        con.print(f"[yellow]Nothing left to try for: {', '.join(plan.exhausted)}[/yellow]")
    for sk in plan.skipped:
        con.print(f"  [dim]skip {sk.target} {sk.session_id}: {sk.reason}[/dim]")


def _print_round(plan: Plan, ex: Execution) -> None:
    for o in ex.outcomes:
        color = {"reserved": "green", "already": "green", "unconfirmed": "red"}.get(o.status, "yellow")
        extra = f" conflicts with {', '.join(o.conflicts_with)}" if o.conflicts_with else ""
        note = f" ({o.note})" if o.note and o.status != "not_sent" else ""
        con.print(f"  [{color}]{o.status}[/{color}] {o.target} {o.session_id}"
                  f"{' ' + o.code if o.code else ''}{extra}{note}")
    if ex.error:
        con.print(f"[red]{ex.error}[/red]")


_RULES_OPTION = typer.Option(None, "--rules", help="Use this rules file instead.")


@app.command()
def book(event: str = config.DEFAULT_EVENT,
         dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan, send nothing."),
         yes: bool = typer.Option(False, "--yes", help="Skip the confirmation."),
         rules_file: Path | None = _RULES_OPTION):
    """Reserve your rules' targets in priority order, falling back on full sessions."""
    r, c, st = _rules(rules_file), _client(), _store()
    st.apply_sweep(event, list(c.iter_sessions(event, include_abstracts=False)), with_abstracts=False)
    for m in rules_mod.unresolved(r, st, event):
        con.print(f"[yellow]unresolved[/yellow] {m}")
    held = c.get_schedule(event).reserved
    router = Router(r, st, event)
    plan = router.plan(held, quota_left=c.quota.remaining(RESERVE_OP))
    _print_plan(plan, st, event)
    if dry_run or not plan.batch:
        if not plan.batch:
            con.print("Nothing to reserve.")
        return
    if not yes and not typer.confirm(f"Reserve these {len(plan.batch)} sessions?"):
        raise typer.Exit(1)
    run = run_booking(router, c, st, event, held, on_round=_print_round)
    if run.closed:
        con.print(f"[yellow]{WRITES_CLOSED}[/yellow]")
        raise typer.Exit(3)
    got = [o for o in run.outcomes if o.status in ("reserved", "already")]
    con.print(f"Reserved {len(got)} of {len(r.targets)} targets. "
              f"GetSchedule now lists {len(run.schedule.reserved) if run.schedule else '?'} reserved.")
    if any(e.disagreements for e in run.executions):
        con.print("[red]Read-back disagreed with the API response. See the lines marked unconfirmed.[/red]")
        raise typer.Exit(4)


@app.command()
def cancel(session_id: str, event: str = config.DEFAULT_EVENT,
           yes: bool = typer.Option(False, "--yes", help="Skip the confirmation if the band is open.")):
    """Cancel one reservation. Shows the seat band first and asks before cancelling."""
    c, st = _client(), _store()
    chk = cancel_engine.check(c, event, session_id)
    s = chk.session
    con.print(f"{s.abbreviation or ''} {s.title}  [band: {chk.band}]")
    if not chk.held:
        con.print("You do not hold this session. Nothing to cancel.")
        raise typer.Exit(1)
    if not chk.band_open:
        con.print("[yellow]The band is not open. If you cancel, you may not get this seat back.[/yellow]")
        if yes:
            con.print("Refusing --yes while the band is not open. Run without --yes to confirm by hand.")
            raise typer.Exit(1)
    if not yes and not typer.confirm("Cancel this reservation?"):
        raise typer.Exit(1)
    res = cancel_engine.cancel(c, st, event, session_id)
    color = {"cancelled": "green", "not_held": "yellow", "closed": "yellow"}.get(res.status, "red")
    con.print(f"[{color}]{res.message}[/{color}]")
    raise typer.Exit({"cancelled": 0, "not_held": 1, "closed": 3}.get(res.status, 4))


if __name__ == "__main__":
    app()
