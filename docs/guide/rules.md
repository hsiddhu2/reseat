# Rules file

`~/.reseat/rules.yaml` lists targets in priority order. re:Seat reserves nothing else. re:Seat writes it readable by you only, because it can hold `serve_secret`, and refuses to write through a symbolic link. If you made the file yourself and others on the machine can read a secret in it, re:Seat says so and gives the `chmod` to run.

If you built your schedule in the AWS portal, `reseat rules from-schedule` writes the file for you. Your reserved sessions come first, then your favorites, in the order the API lists them. The API does not promise that order, so reorder the targets to set your priority. Each target is the exact sitting you picked, by session id with `repeats: false`, so the file can be written even while the catalog is empty. `favorites sync` and `book` still need `reseat sync` first. Every reserved session is listed. A favorite past `watch_cap` is written as a comment, not dropped. The API treats a favorite as interest only, not a reservation. This command turns your favorites into booking targets on purpose, and says how many, so delete any you only want to keep an eye on. When an exact sitting is full and the talk has other sittings, `book` says so in one line. Nothing moves to another sitting unless you set `repeats: true` on that target. Other settings are the `rules init` defaults, with its example lunch left as a comment. Without `--force` it never replaces an existing file. With `--force` the old file is kept as `rules.yaml.bak`, because its settings, `serve_secret` included, are reset.

```yaml
targets:
  - code: ARC301          # any sitting of ARC301. Earliest first unless prefer: latest
    backups: [ARC302]     # tried if no sitting of ARC301 can be held
  - session_id: 1780442277219001cKCh
    repeats: false        # this sitting only
meals:
  - {day: Tuesday, start: "12:00", end: "13:00"}
max_per_day: 5
arrive_confidence: 0.8      # leave early enough to finish the estimated walk in time this often (0.5 to 0.99)
watch_cap: 25
home_venue: Venetian      # where the first walk of each day starts
serve_secret: <long random string>   # needed for the web app off this laptop
ntfy_topic: <random 16 to 64 characters>   # optional push
probe_session: <id of a session that takes no reservations>   # resume the moment writes open
```

The first target wins a time slot. Within a batch, formats that are not recorded go first, because they fill first: Workshop, Lab and Bootcamp, then Builders' session, Chalk talk, Code talk, Breakout session, then the rest.

[Back to the README](../../README.md)
