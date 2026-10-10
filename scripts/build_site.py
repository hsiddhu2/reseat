"""Build the static click-through demo for GitHub Pages.

    python scripts/build_site.py _site

Runs demo mode by hand, exactly as `reseat serve --demo` scripts it: a baseline
sweep, a seat opens, a new sitting appears, a room moves. Then it saves the three
views as plain HTML, approves the swap through the real swap code on the fake API,
and saves the dashboard again. No server, no JavaScript, no sign-in. Every page
says it is a snapshot of demo data.
"""

from __future__ import annotations

import re
import sys
from importlib import resources
from pathlib import Path

from reseat import demo as D
from reseat import pages

NOTE = ('Static snapshot of demo data, from <code>reseat serve --demo</code>. Nothing here talks to AWS. '
        'Swap now shows the result the real swap code produced. Run it live: '
        '<code>pip install -e .</code> then <code>reseat serve --demo</code>.')
EXTRA_CSS = """
/* static snapshot only */
.actions a { display: inline-flex; align-items: center; justify-content: center; min-height: 44px;
  padding: 10px 14px; border-radius: 6px; text-decoration: none; font-weight: 600; }
.actions a.primary { background: var(--accent); color: #fff; flex: 1 1 auto; }
.actions a.secondary { border: 1px solid var(--line); background: var(--surface); color: var(--ink);
  font-weight: 400; }
.demo code { font-weight: 600; }
.done { background: var(--surface); border: 1px solid var(--good); border-radius: 10px; padding: 12px 14px; }
"""


def staticize(html: str, now: float, done: str | None = None) -> str:
    """Point links and assets at sibling files, drop the script, make the buttons links."""
    html = html.replace('<script src="/static/app.js" defer></script>', "")
    html = html.replace('href="/static/app.css"', 'href="app.css"')
    html = html.replace('href="/approve"', 'href="approve.html"')
    html = html.replace('href="/today"', 'href="today.html"')
    html = html.replace('href="/"', 'href="index.html"')
    html = re.sub(r'<button class="primary" data-approve="[^"]+">([^<]*)</button>',
                  r'<a class="primary" href="after.html">\1</a>', html)
    html = re.sub(r'<button data-skip="[^"]+">([^<]*)</button>',
                  r'<a class="secondary" href="index.html">\1</a>', html)
    html = re.sub(r'<b data-until="(\d+)">…</b>',
                  lambda m: f"<b>{max(0, int(m.group(1)) - int(now))} s</b>", html)
    banner = f'<div class="demo" role="status">{NOTE}</div>'
    html = re.sub(r'<div class="demo" role="status">.*?</div>', banner, html, count=1, flags=re.S)
    if done:
        html = html.replace('<aside class="side">', f'<aside class="side"><p class="done">{done}</p>', 1)
    return html


def build(out: Path) -> list[str]:
    d = D.Demo(port=0)
    d.watcher.tick()
    d.seat_opens()
    d.watcher.tick()
    d.new_sitting()
    d.watcher.tick()
    d.room_moves()
    d.watcher.tick()
    d.app.refresh()
    out.mkdir(parents=True, exist_ok=True)
    snap = d.app.snapshot()
    now = snap["now"]
    written = {
        "index.html": pages.render_dashboard(snap, demo=True),
        "approve.html": pages.render_approve(snap, demo=True),
        "today.html": pages.render_today(snap, demo=True),
    }
    [p] = d.watcher.pending()
    res = d.watcher.approve(p.plan_id)
    d.app.refresh()
    got = d.store.get(D.EVENT, res.wanted_id)
    done = pages.e(f"Swap now ran: {' → '.join(res.steps)}. Every step is in the journal below. "
                   f"{got.abbreviation if got else 'The new session'} is held now.")
    written["after.html"] = staticize(pages.render_dashboard(d.app.snapshot(), demo=True), now, done)
    for name in ("index.html", "approve.html", "today.html"):
        written[name] = staticize(written[name], now)
    for name, html in written.items():
        (out / name).write_text(html, encoding="utf-8")
    css = resources.files("reseat").joinpath("static", "app.css").read_text(encoding="utf-8")
    (out / "app.css").write_text(css + EXTRA_CSS, encoding="utf-8")
    d.watcher.stop()
    return sorted(written)


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "_site")
    print("wrote", ", ".join(build(target)), "to", target)
