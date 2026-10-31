# Running it all week

re:Seat is designed for a laptop left in the hotel room, plugged in and awake, while you carry only your phone. Your sign-in can only happen on your own machine, so the laptop makes every API call. The phone gets notifications through the ntfy app and approves a swap from the notification. Opening the web app on the phone is optional, over Tailscale.

**Keep the laptop awake with the lid closed.** Start it with `reseat serve`.

- macOS. `caffeinate -s reseat serve` keeps the Mac awake while it is plugged in. Closing the lid still puts most MacBooks to sleep unless an external display is attached. Either leave the lid open with the screen dimmed, or run `sudo pmset -a disablesleep 1` before you leave and `sudo pmset -a disablesleep 0` when you are back.
- Windows. Settings, System, Power and battery: when plugged in, sleep after Never. Control Panel, Power Options, Choose what closing the lid does: when plugged in, Do nothing.

**Set up the phone: notifications.** Set `ntfy_topic` in the rules file to a long random name, install the ntfy app on the phone and subscribe to that topic. Push is off unless the topic is set. The laptop posts to `https://ntfy.sh/<topic>`, each kind once:

- **Swaps:** a swap proposed, 2 minutes from lapsing, done, rolled back or failed.
- **Bookings:** a seat booked. A Book offer: a session just added, or a seat opened on a favorite, that fits your day. More than five new sessions in one sweep come as five offers and one summary. A seat opened or a new sitting appeared but your rules stopped it, with the reason. The only sitting your rules allow is full while another has seats. A booking the read-back could not confirm. The catalog back after the API served none.
- **Schedule:** a session you hold moved or left the catalog. Two held sessions too far apart to walk between in the gap. The leave-now blocks re:Seat wrote no longer match what you hold, with, under `ntfy_approve: true`, an **Update** button that rewrites them and reads the schedule back (only if you use the blocks).
- **On site:** a morning briefing between 06:00 and 10:00 on each conference day, leave in 10 minutes, leave now, and a talk you want but hold no seat for starting in 20 minutes, with whether to queue.
- **Health:** writes paused or resumed, the API unreachable for 10 minutes or back, sign-in needed.

Each push carries the event, session codes, titles and those status lines, never your token, an abstract or a session id. When `reseat serve` listens on an address other than 127.0.0.1, a tap opens the right page there. Use your Tailscale address: it works only inside your own Tailscale network.

**Choose what reaches the phone.** Under `notify:` in the rules file, `quiet: "22:00-07:00"` (Las Vegas time) holds pushes back and sends one summary when the quiet hours end, and `mute: [on_site]` drops a category: swaps, bookings, schedule, on_site, health. Sign in needed and a swap that failed always come through.

**Approve from the notification.** Add `ntfy_approve: true` and a swap proposal, and its 2-minute reminder, arrive with **Swap now** and **Keep** buttons, with `ntfy_book: true` a Book offer with **Book** and **Dismiss** (at most 3 bookings a day from the notification), and out-of-date leave-now blocks with **Update**. Long-press the notification to see them. The tap goes back through ntfy.sh to a second topic derived from yours, and re:Seat reads it over an outgoing connection, so the phone never has to reach the laptop: no Tailscale, no link, no sign-in. It runs the same checked swap as the web app, and a second push tells you the result. The button carries the proposal's plan id, which is random, works once and expires after 10 minutes, so tapping twice runs the swap once. To try it first on demo data: `reseat serve --demo --push <your topic>`.

**Your topic name is the secret.** ntfy.sh topics have no password: anyone who knows the name can read what is posted to it and post to it. So treat `ntfy_topic` like a password. Make it long and random, for example `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`, never reuse it, and do not put it in a screenshot. re:Seat accepts only 16 to 64 letters, digits, `-` and `_`, and on macOS and Linux warns when a rules file holding the topic can be read by other users on the machine.

If the name leaks, this is what someone else could do, and what limits it:

- **Read your pushes.** They hold session codes, titles, times, rooms, venues and status lines. When the web app is reachable from your phone, they also hold its address on your Tailscale network, and with `ntfy_approve` each button's single-use id. Never your token, an abstract, a session id, or anything that signs in to AWS. Knowing the topic gives no access to your schedule or your account.
- **Post to your topic.** A fake notification could appear on your phone. re:Seat never acts on what is posted there. Check anything odd in the web app or the official app.
- **With `ntfy_approve` or `ntfy_book` on, tap your buttons.** With `ntfy_approve: true` they could approve or dismiss a swap that is waiting for you, or run Update on your leave-now blocks. With `ntfy_book: true` they could book or dismiss a Book offer that is waiting. That is all. A button only works on a proposal or offer re:Seat made, while it waits: its plan id is random, works once and expires after 10 minutes. Each Book tap reserves the one session it offered, after the same checks as the web app: the seat is open, it fits around what you hold, and it is not a second sitting of a talk you hold. Each offer could still become a seat you did not want, which may block a target that opens later at that time, so bookings from the notification stop at 3 a day. Swap now runs the same checked swap as the web app: a fresh read of the new seat, a fallback for yours, and your seat booked again if the new one is refused. Update only adds, changes or removes the leave-now blocks re:Seat wrote. Nobody can use the topic to book, cancel or favorite anything else.
- **What to do.** Put a new random name in `ntfy_topic`, subscribe to it in the ntfy app, unsubscribe from the old one, and restart `reseat serve`. re:Seat no longer posts to or listens on the old name. ntfy.sh keeps past messages for a while, so pushes already sent stay readable on the old name until they expire there. To keep push but drop the buttons, remove `ntfy_approve` and `ntfy_book`.

**When writes are switched off.** A 409 means the API has reservation writes turned off, and retrying will not help until they are back. re:Seat keeps watching and keeps every opening queued, but sends no reserve and runs no swap. It tries once after 15 minutes. With `probe_session` set to a session that takes no reservations, it checks every minute instead and resumes the moment writes open. Push says "Booking paused" and "Booking resumed".

**On site.** During the event days the laptop also checks the day's held and wanted sessions every 20 seconds, at most 35 of them, so a seat freed by a no-show is booked while you are in the walk-up line.

**When the hotel Wi-Fi drops.** The watcher keeps running. It retries with a growing wait, up to 5 minutes between tries, and goes back to its normal pace as soon as a sweep works. A sweep that fails halfway is not saved, so a seat that opened during the outage is still caught afterwards. Each outage goes in the journal. Your reserved seats stay reserved, because they live in your AWS schedule, not on the laptop. While the laptop is offline it cannot book anything and the phone cannot reach it, so use the official app until it is back. If the AWS API is down but the internet is up, push tells you "re:Seat offline since HH:MM" after 10 minutes and "re:Seat back" when it recovers.

**When sign-in expires.** If the token can no longer be refreshed, re:Seat stops booking, keeps watching for the moment it can read again, and tells you once: "sign in needed". Run `reseat login` on the laptop. Booking resumes on the next sweep.

### Optional: the web app on the phone

**Set it up.** Install [Tailscale](https://tailscale.com) on the laptop and the phone and sign in to the same account on both. Add a long random `serve_secret` to the rules file, then start it on the laptop:

```bash
reseat serve --tailscale
```

re:Seat listens on 127.0.0.1 only and asks Tailscale to forward `http://<your laptop's Tailscale name>:8490` to it, so the page is reachable from your own Tailscale devices and nowhere else. Stopping `reseat serve` removes the forwarding. This also avoids macOS's Local Network rules, which can drop connections from other devices to a program they do not recognise. `--host <Tailscale address>` still works where those rules allow it.

It prints a one-time link. Open it once on each device. It sets a cookie and the address bar is left clean. If the cookie is lost, open `/login` on the same address and type the secret. Wrong secrets are limited to 5 a minute from each device and 30 a minute from all devices together, so one device guessing does not lock out your phone. A sign-in lasts 7 days. To sign every device out, stop and restart `reseat serve`: sign-ins live only in the running process. The page is plain HTTP, so open it over Tailscale, which encrypts the connection, not across open hotel Wi-Fi. Without `serve_secret`, `reseat serve` listens on 127.0.0.1 only and refuses any other address. It never listens on every interface: `--host 0.0.0.0` is refused. The page is access control for a hotel network, not a security product: it stops a neighbour on the same Wi-Fi from approving your swaps. Swap now and Dismiss take a plan id that expires after 10 minutes. No page or endpoint takes a session id.

**Reach it from the phone.** With `--tailscale`, open the printed link on the phone. Tailscale is designed to let the phone reach the laptop from any venue, and the page is not offered on the hotel network.

**If the phone cannot open it.** `reseat serve` checks it can reach its own address when it starts. On macOS, a terminal app without Local Network permission accepts connections and then drops them. Allow the app you run reseat in (Terminal, iTerm or VS Code) under System Settings, Privacy & Security, Local Network, then quit and reopen it. Also check Tailscale is switched on on both devices.

**Put it on the home screen.** On the iPhone, open the web app in Safari, tap Share, then Add to Home Screen. re:Seat then opens full screen with its own icon, on Approve. If it asks you to sign in, open `/login` and enter `serve_secret`.

[Back to the README](../../README.md)
