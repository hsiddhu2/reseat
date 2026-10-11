"""Reach the web app from the phone through Tailscale Serve.

`reseat serve --tailscale` listens on 127.0.0.1 only, then asks Tailscale to
forward http://<this machine's Tailscale name>:<port> to it. The phone opens
that name. Nothing listens on a network interface, so the operating system's
local network rules never get in the way, and the page is only reachable from
the attendee's own Tailscale network.

Rules this module exists to respect:
- It changes only the one forwarding it adds, on the port re:Seat uses, and
  removes it when re:Seat stops. It refuses to replace a forwarding someone
  else set up on that port.
- It runs the Tailscale command-line tool with fixed arguments. Nothing from
  the network or the rules file reaches the command line except the port.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

MAC_APP = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class TailnetError(Exception):
    """Tailscale cannot forward to re:Seat. The message says what to fix."""


def _run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=20, check=False)


def find_cli() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    return str(MAC_APP) if MAC_APP.exists() else None


@dataclass
class Tailnet:
    cli: str
    run: Runner = _run

    @classmethod
    def locate(cls, run: Runner = _run) -> Tailnet:
        cli = find_cli()
        if not cli:
            raise TailnetError("Tailscale is not installed. Install it from tailscale.com on this computer "
                               "and on the phone, and sign in to the same account on both.")
        return cls(cli, run)

    def _call(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return self.run([self.cli, *args])
        except (OSError, subprocess.SubprocessError) as e:
            raise TailnetError(f"Could not run Tailscale: {e}") from None

    def name(self) -> str:
        """This machine's Tailscale name, such as my-laptop.tail1234.ts.net."""
        r = self._call("status", "--json")
        try:
            status = json.loads(r.stdout or "{}")
        except ValueError:
            status = {}
        if r.returncode != 0 or status.get("BackendState") != "Running":
            raise TailnetError("Tailscale is not connected on this computer. Open Tailscale and sign in, "
                               "then try again.")
        name = ((status.get("Self") or {}).get("DNSName") or "").rstrip(".")
        if not name:
            raise TailnetError("Tailscale has no name for this computer. Turn on MagicDNS in the Tailscale "
                               "admin console, then try again.")
        return name

    def forward(self, port: int) -> None:
        """Forward the tailnet's http://<name>:<port> to 127.0.0.1:<port>, in the background."""
        current = self._call("serve", "status", "--json")
        if current.returncode == 0 and _foreign(port, current.stdout):
            raise TailnetError(f"Tailscale already forwards port {port} somewhere else. Pick another port "
                               "with --port, or remove that forwarding with tailscale serve.")
        r = self._call("serve", "--bg", f"--http={port}", f"http://127.0.0.1:{port}")
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip().splitlines()[:1]
            why = f": {detail[0]}" if detail else "."
            raise TailnetError("Tailscale would not forward to re:Seat" + why)

    def stop(self, port: int) -> bool:
        """Remove the forwarding added by `forward`. Best effort: re:Seat is stopping anyway."""
        try:
            return self._call("serve", f"--http={port}", "off").returncode == 0
        except TailnetError:
            return False


def _foreign(port: int, status_json: str | None) -> bool:
    """True when Tailscale already uses `port` for something other than re:Seat's own forwarding."""
    try:
        cfg = json.loads(status_json or "{}") or {}
    except ValueError:
        return False                   # unreadable: let the forward command decide
    if str(port) not in (cfg.get("TCP") or {}):
        return False
    ours = f"http://127.0.0.1:{port}"
    for key, site in (cfg.get("Web") or {}).items():
        if key.rsplit(":", 1)[-1] == str(port):
            proxies = [h.get("Proxy") for h in ((site or {}).get("Handlers") or {}).values()]
            return not proxies or any(p != ours for p in proxies)
    return True                        # the port is taken by a forwarding that is not a web one
