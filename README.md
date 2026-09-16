# Syncshot

Keep your git repository in sync.

Syncshot allows you to use a remote Git repo for simple syncing. It's useful for backing up folders of markdown, CSV data, and anything where backups are valuable, but you don't want to handle manually committing and pushing.

This repository is synced using the tool itself. The tool is complete. It runs using only the Python standard library and shells out to Git.

## Running Syncshot

Syncshot is a single file script, so you can run it by grabbing the file from GitHub.

```sh
curl https://raw.githubusercontent.com/t-eckert/syncshot/refs/heads/main/syncshot.py | python3 
```

If you clone it down first, call the script using Python from whatever directory you are working in.

```sh
python3 /path/to/syncshot.py
```

Syncshot accepts the following flags:

- `--period [int]`: A positive integer representing the number of seconds between sync attempts.
- `--debug`: Turns on debug logging.

By default the period is 10 seconds and debug logging is turned off.

## Pausing

Syncing every ten seconds is the wrong behaviour in the middle of a big change. A bulk rename lands as a dozen half-finished commits, and anything that pulls while it is underway gets a broken intermediate state.

```sh
python3 syncshot.py pause 20s   # hold off syncing (default: 5m)
python3 syncshot.py resume      # sync again now
python3 syncshot.py status      # paused? by whom? for how long?
```

A pause is a lease: a file named `<deadline>.<owner>` under `.git/syncshot-pause.d`. Syncing is paused while any unexpired lease exists, and Syncshot sweeps expired ones as it goes.

Two properties are deliberate.

**Leases expire**, and none may last longer than an hour. A pause you forget about is worse than the noisy commits it prevents, because stalled syncing is invisible while noisy commits are not.

**Each holder gets its own file**, so several people or agents can pause at once without clobbering each other. `resume` releases only your own leases; use `--all` to release everyone's. Set `SYNCSHOT_OWNER` or pass `--owner` to name yourself, which is what `status` reports.

Because the leases live in the git dir, `git add .` can never stage them.

### Holding for one command

When you know the command, `hold` is better than guessing a duration. It pauses for exactly as long as the command runs and releases afterwards, including when the command fails or you interrupt it.

```sh
python3 syncshot.py hold -- git mv Projects/Thing Fields/Thing/Projects
```

The exit status is the command's own, so `hold` drops into a script without changing its behaviour. Under the hood it takes a short lease and renews it while the command runs, so a long command stays paused while one that is killed outright stops blocking syncing within half a minute. That renewal is also why `hold` is not subject to the one hour cap: a process still renewing is demonstrably alive, which is what the cap is there to check.

Use `pause` when there is no single command to wrap, such as an agent making a change across many steps.

## Logging

At the default level Syncshot logs events, not ticks: it starts, it commits, it pushes, it pulls, it says why it is on hold and when that clears, and it reports anything that fails. An idle day is one line. `--debug` adds the per-period detail, which is what the default used to be.

Anything that lasts for many periods, like a pause or an unresolved rebase, is reported when it starts and when it ends rather than on every attempt. Repeats go to debug, so the event that caused it is not buried under identical lines.

Daemon output carries a timestamp, since it is usually read later out of a file. The one-shot subcommands do not, since they are read as they are typed.


