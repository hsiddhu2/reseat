"""re:Seat command line.

Week one commands. Reads are live today. Writes to reservations open 8 October.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Column, Table

from . import auth, config, fixtures
from . import cancel as cancel_engine
from . import favorites as favorites_engine
from . import guard as guard_engine
from . import rules as rules_mod
from .client import ApiError, EventsClient, NotRegistered, OperationClosed
from .router import OP as RESERVE_OP
from .router import WRITES_CLOSED, Execution, Outcome, Plan, Router, run_booking
from .store import Store, SweepRefused
from .swap import Swap, SwapBusy, SwapResult
from .watcher import Proposal, Watcher, WatchError, WatchEvent

app = typer.Typer(help="re:Seat keeps your re:Invent seats.", no_args_is_help=True)
rules_app = typer.Typer(help="Create, import and check your rules file.", no_args_is_help=True)
app.add_typer(rules_app, name="rules")
guard_app = typer.Typer(help="Leave-now blocks in your official schedule.", no_args_is_help=True)
app.add_typer(guard_app, name="guard")
favorites_app = typer.Typer(help="Mirror your rules into the official favorites.", no_args_is_help=True)
app.add_typer(favorites_app, name="favorites")
con = Console(emoji=False)   # ":word:" in a session title must stay text


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _esc(v: object) -> str:
    """Catalog titles, personal time and API messages are not ours: show "[" as text, never as
    markup, and drop control characters so no text can recolour the terminal. Newlines stay."""
    return escape(_CONTROL.sub("?", str(v)))


def _row(t: Table, *cells: str) -> None:
    t.add_row(*(_esc(c) for c in cells))


def _code(st: Store | None, event: str, sid: str, when: bool = False) -> str:
    """The sitting code a person reads, such as ARC302-R1. The id when the session is not stored."""
    s = st.get(event, sid) if st else None
    if not s:
        return sid
    t = s.session_time
    at = f" ({t.date} {t.time})" if when and t and t.date else ""
    return f"{s.abbreviation or sid}{at}"


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
        con.print(f"[red]{_esc(e)}[/red]")
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
        con.print(f"[red]Signed in, but this Builder ID is not registered for {_esc(event)}.[/red]")
        raise typer.Exit(2) from None
    except ApiError as e:
        con.print(f"[red]{_esc(e)}[/red]")
        raise typer.Exit(1) from None
    con.print(f"[green]Registered for {_esc(event)}.[/green] "
              f"{len(s.reserved)} reserved, {len(s.favorites)} favorites, "
              f"{len(s.personal_time)} personal time entries.")


# --------------------------------------------------------------------------- catalog


@app.command()
def events(past: bool = typer.Option(False, help="Include events that already ended.")):
    """List AWS events. Needs no sign-in."""
    c = _client()
    t = Table("ID", "Name", "Start", "End", "Auth")
    for e in c.list_events(include_past=past):
        _row(t, e.event_id, e.name, e.start_date[:10], e.end_date[:10],
                  "yes" if e.authentication_required else "no")
    con.print(t)


@app.command()
def sync(event: str = config.DEFAULT_EVENT,
         abstracts: bool = typer.Option(True, help="Fetch abstracts. Off for a cheap sweep."),
         force: bool = typer.Option(False, "--force", help="Apply even an empty or much smaller catalog.")):
    """Pull the whole catalog into the local store and report what changed."""
    c, st = _client(), _store()
    t0 = time.time()
    sessions = list(c.iter_sessions(event, include_abstracts=abstracts))
    try:
        res = st.apply_sweep(event, sessions, with_abstracts=abstracts, force=force)
    except SweepRefused as e:
        con.print(f"[yellow]{_esc(e)}[/yellow] Otherwise run again later.")
        raise typer.Exit(4) from None
    if res.reason == "rekeyed":
        con.print(f"[yellow]The catalog was re-keyed: {len(res.removed)} sessions replaced. Recorded as a "
                  "new baseline.[/yellow]")
    elif res.reason == "forced":
        con.print(f"[yellow]Applied as asked with --force: {res.count} sessions now stored.[/yellow]")
    if res.baseline and config.RULES_PATH.exists():
        dead = rules_mod.unresolved(_rules(), st, event)
        if dead:
            con.print(f"[yellow]{len(dead)} ids in the rules file are not in the catalog. "
                      "Run reseat rules check.[/yellow]")
    con.print(f"Synced {res.count} sessions in {time.time() - t0:.1f}s. "
              f"Added {len(res.added)}, removed {len(res.removed)}, moved {len(res.moved)}, "
              f"band changes {len(res.band_changes)}, newly open {len(res.opened)}.")
    for sid in res.added[:20]:
        s = st.get(event, sid)
        if s:
            con.print(f"  [green]new[/green] {_esc(s.abbreviation or '')} {_esc(s.title)}")
    for sid in res.opened[:20]:
        s = st.get(event, sid)
        if s:
            con.print(f"  [cyan]opened[/cyan] {_esc(s.abbreviation or '')} {_esc(s.title)} "
                      f"-> {_esc(s.seat_availability)}")


@app.command()
def search(text: str, event: str = config.DEFAULT_EVENT, limit: int = 30):
    """Search the local catalog by title, code or abstract."""
    st = _store()
    t = Table("Code", "Title", "Type", "When", "Venue", "Seats")
    for s in st.search(event, text, limit):
        st_ = s.session_time
        when = f"{st_.date} {st_.time}" if st_ and st_.date else ""
        _row(t, s.abbreviation or "", s.title[:60], s.type or "", when, s.campus_venue or "",
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
    con.print(f"[bold]{_esc(s.abbreviation)}  {_esc(s.title)}[/bold]")
    con.print(f"{_esc(s.type)} | {_esc(s.level)} | {_esc(s.campus_venue)} | {_esc(s.room_label)}")
    if s.session_time:
        con.print(f"{_esc(s.session_time.date)} {_esc(s.session_time.time)} for {s.session_time.length} min")
    con.print(f"Reservable: {s.is_reservable}  Seats: {_esc(s.seat_availability)}")
    if s.base_code:
        reps = [r for r in st.by_base_code(event, s.base_code) if r.session_id != s.session_id]
        if reps:
            con.print("Repeats:")
            for r in reps:
                con.print(f"  {_esc(r.abbreviation)} {_esc(r.session_time.date if r.session_time else '')} "
                          f"{_esc(r.session_time.time if r.session_time else '')} {_esc(r.campus_venue)} "
                          f"\\[{_esc(r.seat_availability)}]  id={_esc(r.session_id)}")
    hist = st.band_history(event, session_id)
    if hist:
        con.print("Band history:")
        for ts, old, new in hist[-10:]:
            when = datetime.fromtimestamp(ts, UTC).strftime("%m-%d %H:%M")
            con.print(f"  {_esc(when)}  {_esc(old or '-')} -> {_esc(new or '-')}")
    if s.abstract:
        con.print(f"\n{_esc(s.abstract)}")


@app.command("save-fixture")
def save_fixture(event: str = config.DEFAULT_EVENT,
                 out: str = typer.Option("", help="Path. Default tests/fixtures/catalog-<today>.json.")):
    """Save a catalog pull for tests, without abstracts or speaker names."""
    path = Path(out) if out else fixtures.default_path(Path.cwd())
    data = fixtures.pull_catalog(_client(), event)
    fixtures.save_catalog(data, path)
    con.print(f"Saved {data['totalCount']} sessions to {_esc(path)}")


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
        t = Table("Code", Column("Title", no_wrap=True, overflow="ellipsis"), "When", "Venue", "Seats",
                  title=f"{label} ({len(ids)})")
        for sid in ids:
            _row(t, *row(sid))
        con.print(t)
    t = Table("ID", "Start UTC", "End UTC", "Title", "Location", title="Personal time")
    for p in s.personal_time:
        _row(t, p.personal_time_id, p.start_date_time, p.end_date_time, p.title, p.location or "")
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
            con.print(f"would favorite {_esc(batch)}")
            continue
        try:
            res = c.favorite(event, batch)
        except OperationClosed:
            st.journal(event, "AssociateFavorites", batch, {"status": 409}, "closed")
            con.print("[yellow]Favorites are closed (409). Nothing was changed.[/yellow]")
            raise typer.Exit(3) from None
        except ApiError as e:
            st.journal(event, "AssociateFavorites", batch, {"error": str(e)}, "error")
            con.print(f"[red]{_esc(e)}. Stopped. Reading back below.[/red]")
            break
        outcome = "partial" if res.failed else "success"
        st.journal(event, "AssociateFavorites", batch, res.model_dump(), outcome)
        for sid in res.successful:
            con.print(f"[green]favorited[/green] {_esc(sid)}")
        for f in res.failed:
            con.print(f"[yellow]{_esc(f.session_id)}: {_esc(f.code)}[/yellow]")
    if not dry_run:
        back = c.get_schedule(event)
        missing = [i for i in ids if i not in back.favorites]
        st.journal(event, "GetSchedule", ids, back.model_dump(by_alias=True),
                   "readback-missing" if missing else "readback-ok")
        if missing:
            con.print(f"[red]Read-back does not list as favorites: {_esc(', '.join(missing))}[/red]")


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
    path = path or config.RULES_PATH
    try:
        r = rules_mod.load(path)
    except rules_mod.RulesError as e:
        con.print(f"[red]{_esc(e)}[/red]")
        raise typer.Exit(2) from None
    if warning := rules_mod.exposure_warning(path, r):
        con.print(f"[yellow]{_esc(warning)}[/yellow]")
    return r


@rules_app.command("init")
def rules_init(force: bool = typer.Option(False, help="Overwrite an existing rules file.")):
    """Write a commented example rules file."""
    config.ensure_home()
    if config.RULES_PATH.exists() and not force:
        con.print(f"{_esc(config.RULES_PATH)} exists. Use --force to overwrite it.")
        raise typer.Exit(1)
    try:
        written = rules_mod.write_new(config.RULES_PATH, rules_mod.EXAMPLE, replace=force)
    except rules_mod.RulesError as e:
        con.print(f"[red]{_esc(e)}[/red]")
        raise typer.Exit(2) from None
    if not written:
        con.print(f"{_esc(config.RULES_PATH)} exists. Use --force to overwrite it.")
        raise typer.Exit(1)
    con.print(f"Wrote {_esc(config.RULES_PATH)}. Edit it, then run: reseat rules check")


@rules_app.command("import")
def rules_import(file: Path,
                 force: bool = typer.Option(False, help="Replace targets already in the rules file.")):
    """Import targets from a reinvent-planner.cloud JSON export, in its order."""
    try:
        new = rules_mod.from_planner_export(rules_mod.read_planner_file(file))
    except rules_mod.RulesError as e:
        con.print(f"[red]{_esc(e)}[/red]")
        raise typer.Exit(2) from None
    config.ensure_home()
    try:
        rules_mod.refuse_symlink(config.RULES_PATH)  # this command writes, so check before reading
    except rules_mod.RulesError as e:
        con.print(f"[red]{_esc(e)}[/red]")
        raise typer.Exit(2) from None
    if config.RULES_PATH.exists():
        old = _rules()
        if old.targets and not force:
            con.print(f"{_esc(config.RULES_PATH)} already has {len(old.targets)} targets. "
                      "Use --force to replace them. Meals and limits are kept.")
            raise typer.Exit(1)
        try:
            new = rules_mod.Rules(**{**old.model_dump(), "targets": [t.model_dump() for t in new.targets]})
        except ValueError as e:
            con.print(f"[red]{_esc(e)}[/red]")
            raise typer.Exit(2) from None
    try:
        rules_mod.save(new, config.RULES_PATH)
    except rules_mod.RulesError as e:
        con.print(f"[red]{_esc(e)}[/red]")
        raise typer.Exit(2) from None
    con.print(f"Imported {len(new.targets)} targets into {_esc(config.RULES_PATH)}.")


@rules_app.command("from-schedule")
def rules_from_schedule(event: str = config.DEFAULT_EVENT,
                        force: bool = typer.Option(False, "--force",
                                                   help="Overwrite an existing rules file.")):
    """Write targets from your official schedule: reserved first, then favorites."""
    config.ensure_home()
    if config.RULES_PATH.exists() and not force:
        con.print(f"{_esc(config.RULES_PATH)} exists. Use --force to replace it.")
        raise typer.Exit(1)
    try:                                            # refuse before spending an API call
        rules_mod.refuse_symlink(config.RULES_PATH)
        rules_mod.refuse_symlink(config.RULES_PATH.with_name(config.RULES_PATH.name + ".bak"))
    except rules_mod.RulesError as e:
        con.print(f"[red]{_esc(e)}[/red] Nothing written.")
        raise typer.Exit(2) from None
    c, st = _client(), _store()
    try:
        s = c.get_schedule(event)
        imp = rules_mod.from_schedule(s.reserved, s.favorites, st, event)
    except ApiError as e:
        con.print(f"[red]{_esc(e)}[/red] Nothing written.")
        raise typer.Exit(2) from None
    except rules_mod.RulesError as e:
        con.print(f"[red]{_esc(e)}[/red]")
        raise typer.Exit(1) from None
    backup = None
    if config.RULES_PATH.exists():                  # only with --force: keep the old file beside it
        backup = config.RULES_PATH.with_name(config.RULES_PATH.name + ".bak")
    try:
        if backup:
            rules_mod.refuse_symlink(config.RULES_PATH)  # before reading through it
            rules_mod.write_new(backup, rules_mod.read_nofollow(config.RULES_PATH), replace=True)
        written = rules_mod.write_new(config.RULES_PATH, imp.text, replace=force)
    except rules_mod.RulesError as e:
        con.print(f"[red]{_esc(e)}[/red] Nothing written.")
        raise typer.Exit(2) from None
    if not written:
        con.print(f"{_esc(config.RULES_PATH)} appeared while reading the schedule. Nothing written.")
        raise typer.Exit(1)
    con.print(f"Wrote {len(imp.reserved)} reserved and {len(imp.favorites)} favorites as targets "
              f"to {_esc(config.RULES_PATH)}, reserved first. Each is the exact sitting you picked.")
    if backup:
        con.print(f"[yellow]The old file is at {_esc(backup)}. Its settings, such as serve_secret, "
                  "ntfy_topic, meals and limits, were reset to the defaults. Copy back any you need."
                  "[/yellow]")
    if imp.watch_cap > 25:
        con.print(f"watch_cap is {imp.watch_cap} so every reserved session is listed.")
    if imp.left_out:
        con.print(f"[yellow]{len(imp.left_out)} favorites are past watch_cap and are written as "
                  "comments. Raise watch_cap in the file and uncomment them to include them.[/yellow]")
    booked = len(imp.favorites) - len(imp.left_out)
    if booked:
        con.print(f"{booked} favorites are now targets: reseat book and reseat serve will try to "
                  "reserve them. Delete any you only wanted to keep an eye on.")
    for day in imp.full_days:
        con.print(f"[yellow]{_esc(day)} already holds max_per_day reserved sessions, so no favorite "
                  "that day will be booked. Raise max_per_day to allow it.[/yellow]")
    con.print("Next: reseat favorites sync, then reseat book --dry-run.")


@rules_app.command("check")
def rules_check(event: str = config.DEFAULT_EVENT):
    """Validate the rules file against the local catalog and list unresolved codes."""
    r, st = _rules(), _store()
    con.print(f"{len(r.targets)} targets (cap {r.watch_cap}), {len(r.meals)} meals, "
              f"max {r.max_per_day} per day, buffer {r.buffer_minutes} min.")
    missing = rules_mod.unresolved(r, st, event)
    for m in missing:
        con.print(f"  [yellow]unresolved[/yellow] {_esc(m)}")
    if missing:
        con.print("Run reseat sync if the catalog is old, then fix the codes above.")
        raise typer.Exit(1)
    con.print("[green]All targets resolve in the local catalog.[/green]")


@favorites_app.command("sync")
def favorites_sync(event: str = config.DEFAULT_EVENT,
                   dry_run: bool = typer.Option(False, "--dry-run", help="Print what would be sent.")):
    """Add every target sitting to favorites, so the official app shows the same plan."""
    r, c, st = _rules(), _client(), _store()
    res = favorites_engine.sync(c, st, event, r, dry_run=dry_run)
    for o in res.outcomes:
        color = {"added": "green", "already": "dim", "not_sent": "dim"}.get(o.status, "yellow")
        label = "would add" if dry_run and o.status == "not_sent" else o.status
        con.print(f"  [{color}]{label}[/{color}] {_esc(o.target)} {_esc(_code(st, event, o.session_id))}"
                  f"{_esc(' ' + o.code if o.code else '')}")
    con.print(f"{len(res.wanted)} sittings: {res.count('added')} added, {res.count('already')} already "
              f"favorited, {res.count('failed')} failed, {res.count('unconfirmed')} unconfirmed.")
    for d in res.disagreements:
        con.print(f"[red]{_esc(d)}[/red]")
    for label in res.missing:
        con.print(f"[yellow]{label}: not in the local catalog. "
                  "Run reseat sync, then reseat rules check.[/yellow]")
    if res.closed:
        con.print(f"[yellow]{favorites_engine.CLOSED}[/yellow]")
        raise typer.Exit(3)
    if res.error:
        con.print(f"[red]{_esc(res.error)}[/red]")
        raise typer.Exit(4)
    if res.disagreements:
        raise typer.Exit(4)


@guard_app.command("sync")
def guard_sync(event: str = config.DEFAULT_EVENT,
               dry_run: bool = typer.Option(False, "--dry-run", help="Print the changes, send nothing.")):
    """Create, update or delete leave-now blocks so they match what you hold. Idempotent."""
    r, c, st = _rules(), _client(), _store()
    res = guard_engine.sync(c, st, event, r, dry_run=dry_run)
    gp = res.plan
    verb = "would " if dry_run else ""
    for b in gp.create:
        con.print(f"  [green]{verb}create[/green] {_esc(b.title)} {_esc(b.start)}Z  {_esc(b.description)}")
    for _pid, b in gp.update:
        con.print(f"  [cyan]{verb}update[/cyan] {_esc(b.title)} {_esc(b.start)}Z  {_esc(b.description)}")
    for p in gp.delete:
        con.print(f"  [yellow]{verb}delete[/yellow] {_esc(p.title)} {_esc(p.start_date_time)}Z")
    con.print(f"{len(gp.create)} to create, {len(gp.update)} to update, {len(gp.delete)} to delete, "
              f"{len(gp.keep)} already right.")
    for w in gp.warnings:
        con.print(f"[yellow]venue switch:[/yellow] {_esc(w)}")
    for p in res.problems:
        con.print(f"[red]{_esc(p)}[/red]")
    if res.closed:
        raise typer.Exit(3)
    if res.problems:
        raise typer.Exit(4)


# --------------------------------------------------------------------------- booking


def _print_plan(plan: Plan, store: Store, event: str) -> None:
    t = Table("#", "Code", "Type", "When", "Venue", "Target", "Kind", title="Plan, sent in this order")
    for i, p in enumerate(plan.batch, 1):
        s = store.get(event, p.session_id)
        when = f"{s.session_time.date} {s.session_time.time}" if s and s.session_time else ""
        _row(t, str(i), p.code, p.type or "", when, (s.campus_venue if s else "") or "",
                  p.target, "backup" if p.backup else "sitting")
    con.print(t)
    if plan.deferred:
        con.print(f"Over this minute's quota, next round: {_esc(', '.join(p.code for p in plan.deferred))}")
    if plan.held_targets:
        con.print(f"Already held: {_esc(', '.join(plan.held_targets))}")
    if plan.exhausted:
        con.print(f"[yellow]Nothing left to try for: {_esc(', '.join(plan.exhausted))}[/yellow]")
    for sk in plan.skipped:
        con.print(f"  [dim]skip {_esc(sk.target)} {_esc(_code(store, event, sk.session_id))}: "
                  f"{_esc(sk.reason)}[/dim]")


def _print_fallback_hints(router: Router, plans: list[Plan], outcomes: list[Outcome],
                          held: Iterable[str]) -> None:
    for hint in router.fallback_hints(plans, outcomes, held):
        con.print(f"[yellow]{_esc(hint)}[/yellow]")


def _print_round(plan: Plan, ex: Execution, st: Store | None = None,
                 event: str = config.DEFAULT_EVENT) -> None:
    for o in ex.outcomes:
        color = {"reserved": "green", "already": "green", "unconfirmed": "red"}.get(o.status, "yellow")
        extra = (f" conflicts with {', '.join(_code(st, event, c) for c in o.conflicts_with)}"
                 if o.conflicts_with else "")
        note = f" ({o.note})" if o.note and o.status != "not_sent" else ""
        con.print(f"  [{color}]{_esc(o.status)}[/{color}] {_esc(o.target)} "
                  f"{_esc(_code(st, event, o.session_id))}"
                  f"{_esc(' ' + o.code if o.code else '')}{_esc(extra)}{_esc(note)}")
    if ex.error:
        con.print(f"[red]{_esc(ex.error)}[/red]")


_RULES_OPTION = typer.Option(None, "--rules", help="Use this rules file instead.")


@app.command()
def book(event: str = config.DEFAULT_EVENT,
         dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan, send nothing."),
         yes: bool = typer.Option(False, "--yes", help="Skip the confirmation."),
         rules_file: Path | None = _RULES_OPTION):
    """Reserve your rules' targets in priority order, falling back on full sessions."""
    r, c, st = _rules(rules_file), _client(), _store()
    try:
        st.apply_sweep(event, list(c.iter_sessions(event, include_abstracts=False)), with_abstracts=False)
    except SweepRefused as e:
        con.print(f"[red]{_esc(e)}[/red] Not booking from an empty or partial catalog.")
        raise typer.Exit(4) from None
    for m in rules_mod.unresolved(r, st, event):
        con.print(f"[yellow]unresolved[/yellow] {_esc(m)}")
    held = c.get_schedule(event).reserved
    router = Router(r, st, event)
    plan = router.plan(held, quota_left=c.quota.remaining(RESERVE_OP))
    if plan.blocked:
        con.print(f"[red]{_esc(plan.blocked)}[/red]")
        raise typer.Exit(4)
    _print_plan(plan, st, event)
    if dry_run or not plan.batch:
        _print_fallback_hints(router, [plan], [], held)
        if not plan.batch:
            con.print("Nothing to reserve.")
        return
    if not yes and not typer.confirm(f"Reserve these {len(plan.batch)} sessions?"):
        raise typer.Exit(1)
    run = run_booking(router, c, st, event, held, on_round=lambda p, ex: _print_round(p, ex, st, event))
    if run.closed:
        con.print(f"[yellow]{WRITES_CLOSED}[/yellow]")
        raise typer.Exit(3)
    _print_fallback_hints(router, [plan, *run.plans], run.outcomes,
                          run.schedule.reserved if run.schedule else held)
    got = [o for o in run.outcomes if o.status in ("reserved", "already")]
    con.print(f"Reserved {len(got)} of {len(r.targets)} targets. "
              f"GetSchedule now lists {_esc(len(run.schedule.reserved) if run.schedule else '?')} reserved.")
    if any(e.disagreements for e in run.executions):
        con.print("[red]Read-back disagreed with the API response. See the lines marked unconfirmed.[/red]")
        raise typer.Exit(4)


def _print_watch_event(ev: WatchEvent) -> None:
    d = ev.data
    if ev.kind == "sweep":
        when = datetime.fromtimestamp(ev.at).strftime("%H:%M:%S")
        if d["error"]:
            con.print(f"{_esc(when)} [red]sweep failed: {_esc(d['error'])}[/red]. Retrying next tick.")
        else:
            con.print(f"{_esc(when)} {d['count']} sessions, {d['added']} added, {d['opened']} opened, "
                      f"{d['moved']} moved, {d['booked']} booked, {d['proposed']} proposed")
    elif ev.kind == "booked":
        con.print(f"  [green]booked[/green] {_esc(d['target'])} {_esc(d['code'] or d['session_id'])} "
                  f"{_esc(d['title'] or '')}")
    elif ev.kind == "proposed":
        con.print(f"  [cyan]swap proposed[/cyan] {_esc(d['target'])}: "
                  f"{_esc(d['held_id'])} -> {_esc(d['wanted_id'])} "
                  f"plan {d['plan_id']}{' (auto)' if d['auto'] else ''}")
    elif ev.kind == "swap":
        con.print(f"  [cyan]swap {d['state']}[/cyan] plan {d['plan_id']}")
    elif ev.kind == "moved":
        b, a = d["before"], d["after"]
        con.print(f"  [yellow]moved[/yellow] {_esc(d['code'] or d['session_id'])}: "
                  f"{_esc(b.get('date'))} {_esc(b.get('time'))} {_esc(b.get('room'))} -> "
                  f"{_esc(a.get('date'))} {_esc(a.get('time'))} {_esc(a.get('room'))}")
    elif ev.kind == "error":
        con.print(f"  [red]{_esc(d['message'])}[/red]")
    elif ev.kind in ("outage", "offline"):
        con.print(f"  [yellow]{ev.kind}: {_esc(d['message'])}[/yellow]")
    elif ev.kind == "back":
        con.print(f"  [green]back after {d['minutes']} min[/green]")
    elif ev.kind == "signin":
        con.print(f"  [{'red' if d['state'] == 'needed' else 'green'}]{_esc(d['message'])}[/]")


@app.command()
def watch(event: str = config.DEFAULT_EVENT,
          interval: int = typer.Option(60, help="Seconds between sweeps. At least 30."),
          once: bool = typer.Option(False, "--once", help="One sweep, then exit. For cron.")):
    """Watch the catalog and book targets as seats free or new sittings appear."""
    r, c, st = _rules(), _client(), _store()
    def auto_swap(p: Proposal, approved: bool) -> SwapResult:
        res = Swap(c, st, r, event).run_plan(p, approved=approved)
        _print_swap(res, st, event)
        return res

    try:
        w = Watcher(c, st, r, event, interval=interval, swapper=auto_swap)
    except WatchError as e:
        con.print(f"[red]{_esc(e)}[/red]")
        raise typer.Exit(2) from None
    w.subscribe(_print_watch_event)
    if once:
        raise typer.Exit(1 if w.tick().error else 0)
    try:
        w.run()
    except KeyboardInterrupt:
        con.print("Stopped.")


def _print_swap(res: SwapResult, st: Store | None = None, event: str = config.DEFAULT_EVENT) -> None:
    def code(sid: str) -> str:
        return _code(st, event, sid)

    for reason in res.reasons:
        con.print(f"  [yellow]blocked[/yellow] {_esc(reason)}")
    con.print(f"  swap {_esc(code(res.held_id))} -> {_esc(code(res.wanted_id))}: {' -> '.join(res.steps)}")
    if res.alert:
        con.print(f"[bold red]{_esc(res.alert)}[/bold red]")
    con.print(f"  held now: {_esc(', '.join(code(s) for s in res.held_now) or 'nothing')}")


@app.command()
def serve(event: str = config.DEFAULT_EVENT,
          host: str = typer.Option("127.0.0.1", help="Address to listen on. Anything but loopback "
                                   "needs serve_secret in the rules file."),
          port: int = typer.Option(8490, help="Port for the phone page."),
          interval: int = typer.Option(60, help="Seconds between catalog sweeps. At least 30.")):
    """Run the watcher and serve the phone page. Leave the laptop awake and plugged in."""
    import socket

    from . import push
    from . import serve as serve_mod
    r, c, st = _rules(), _client(), _store()

    def swapper(p: Proposal, approved: bool) -> SwapResult:
        return Swap(c, st, r, event).run_plan(p, approved=approved)

    try:
        w = Watcher(c, st, r, event, interval=interval, swapper=swapper)
        a = serve_mod.App(w, st, r, event, host=host, port=port)
        server = serve_mod.make_server(a)
    except (WatchError, serve_mod.ServeError, OSError) as e:
        con.print(f"[red]{_esc(e)}[/red]")
        raise typer.Exit(2) from None
    w.subscribe(_print_watch_event)
    pusher = None
    if r.ntfy_topic:
        def code_title(sid: str) -> str:
            s = st.get(event, sid)
            return f"{s.abbreviation} {s.title}" if s else "a session"
        pusher = push.Pusher(r.ntfy_topic, code_title=code_title)
        pusher.start()
        w.subscribe(pusher)
        con.print("Push is on. Session codes, titles and the event type go to ntfy.sh. Nothing else.")
    if a.auth_required:
        shown = socket.gethostname() if host == "0.0.0.0" else host
        con.print(f"Open this once on your phone. It works one time:\n  {_esc(a.one_time_link(shown))}")
        con.print(f"If you lose it, open http://{_esc(shown)}:{a.port}/login and enter serve_secret.")
        con.print("This is plain HTTP. Reach it over Tailscale, which encrypts the link, not over open "
                  "hotel Wi-Fi. Restarting reseat serve signs every phone out.")
    else:
        con.print(f"Phone page on this laptop only: http://{_esc(host)}:{a.port}/")
    worker = serve_mod.run(a, server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        con.print("Stopping. A swap in progress finishes first.")
    finally:
        w.stop()
        worker.join(timeout=serve_mod.APPROVE_WAIT)
        server.server_close()
        if pusher:
            pusher.stop()


@app.command("mcp")
def mcp_cmd(event: str = config.DEFAULT_EVENT):
    """Run the local MCP server on stdio. Needs: pip install "reseat[mcp]"."""
    from . import mcp_server
    try:
        server = mcp_server.build_server(mcp_server.Tools(_client(), _store(), _rules(), event))
    except ImportError:
        typer.echo('The MCP server needs the extra: pip install "reseat[mcp]"', err=True)
        raise typer.Exit(2) from None
    server.run("stdio")


@app.command()
def swap(held_id: str, wanted_id: str, event: str = config.DEFAULT_EVENT,
         yes: bool = typer.Option(False, "--yes", help="Skip the confirmation.")):
    """Replace a held session with a wanted one. Checks a fallback first, rolls back on failure."""
    r, c, st = _rules(), _client(), _store()
    sw = Swap(c, st, r, event)
    try:
        pre = sw.check(held_id, wanted_id, approved=True)
    except ApiError as e:
        con.print(f"[red]{_esc(e)}[/red]")
        raise typer.Exit(2) from None
    if pre.reasons:
        _print_swap(pre, st, event)
        raise typer.Exit(1)
    named = ", ".join(_code(st, event, f, when=True) for f in pre.fallbacks)
    con.print(f"Checks passed. Fallbacks if this fails: {_esc(named)}.")
    if not yes and not typer.confirm(f"Cancel {_code(st, event, held_id)} and reserve "
                                     f"{_code(st, event, wanted_id)}?"):
        raise typer.Exit(1)
    try:
        res = sw.run(held_id, wanted_id, approved=True)
    except (SwapBusy, ApiError) as e:
        con.print(f"[red]{_esc(e)}[/red]")
        raise typer.Exit(2) from None
    _print_swap(res, st, event)
    raise typer.Exit({"verified": 0, "rolled_back": 4}.get(res.state, 1 if res.reasons else 4))


@app.command()
def cancel(session_id: str, event: str = config.DEFAULT_EVENT,
           yes: bool = typer.Option(False, "--yes", help="Skip the confirmation if the band is open.")):
    """Cancel one reservation. Shows the seat band first and asks before cancelling."""
    c, st = _client(), _store()
    chk = cancel_engine.check(c, event, session_id)
    s = chk.session
    con.print(f"{_esc(s.abbreviation or '')} {_esc(s.title)}  \\[band: {_esc(chk.band)}]")
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
    con.print(f"[{color}]{_esc(res.message)}[/{color}]")
    raise typer.Exit({"cancelled": 0, "not_held": 1, "closed": 3}.get(res.status, 4))


if __name__ == "__main__":
    app()
