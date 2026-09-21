import subprocess
import logging
import re
import os
import math
import time
import getpass
import argparse
import signal

from datetime import datetime, timezone
from pathlib import Path

# Global flag for graceful shutdown
shutdown_requested = False

# A pause is a lease: a file named "<epoch deadline>.<owner>" in this directory
# under the git dir. Syncing is paused while any unexpired lease exists. Every
# writer only ever creates or deletes its own path, so concurrent holders cannot
# clobber each other and no locking is needed.
PAUSE_DIR_NAME = "syncshot-pause.d"

# A lease nobody remembers is worse than the noisy commits it prevents, so every
# lease expires and no single one can last longer than this.
DEFAULT_PAUSE_SECONDS = 300
MAX_PAUSE_SECONDS = 3600

# `hold` renews a short lease while its command runs, so a holder that is killed
# outright stops blocking syncing within this many seconds instead of lingering.
HOLD_LEASE_SECONDS = 30

# Git waits forever on a connection that died without a reset, which is what a
# machine that slept mid-fetch leaves behind. The loop is single threaded, so one
# such call stops syncing altogether until somebody notices it is stale. Every
# call that talks to the remote gets a deadline, and ssh gets keepalives so that
# it usually gives up on its own well before the deadline has to kill anything.
NETWORK_TIMEOUT_SECONDS = 120
SSH_KEEPALIVE = "-o ConnectTimeout=10 -o ServerAliveInterval=10 -o ServerAliveCountMax=3"

# What is currently stopping syncing, if anything. Held between ticks so that a
# pause or a stuck rebase is reported once when it starts and once when it ends,
# rather than every period for as long as it lasts.
blocked_by = None


def main(period):
    setup_signal_handlers()
    logging.info("Syncshot is running")

    while not shutdown_requested:
        logging.debug("Syncing...")
        try:
            sync()
        except subprocess.TimeoutExpired as e:
            command = " ".join(e.cmd) if isinstance(e.cmd, list) else str(e.cmd)
            logging.error(
                f"`{command}` hung for {round(e.timeout)}s and was killed. "
                "Continuing to next sync attempt"
            )
        except subprocess.CalledProcessError as e:
            logging.error(f"An error occurred while syncing: {e}")
            logging.debug(f"Command output: {e.output}")
            logging.debug(f"Command stderr: {e.stderr}")
            logging.debug("Continuing to next sync attempt")

        if shutdown_requested:
            break

        logging.debug("Done")
        logging.debug(f"Sleeping {period}")

        sleep_remaining = period
        while sleep_remaining > 0 and not shutdown_requested:
            sleep_time = min(1, sleep_remaining)
            time.sleep(sleep_time)
            sleep_remaining -= sleep_time

    logging.info("Syncshot shutting down gracefully")


def signal_handler(signum, _):
    """Handle interrupt signals gracefully"""

    global shutdown_requested
    signal_name = signal.Signals(signum).name
    logging.info(
        f"Received {signal_name}. Finishing current sync and shutting down gracefully..."
    )
    shutdown_requested = True


def setup_signal_handlers():
    """Set up signal handlers for graceful shutdown"""

    signal.signal(signal.SIGINT, signal_handler)  # CTRL+C
    signal.signal(signal.SIGTERM, signal_handler)  # Termination signal
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal_handler)  # Hangup signal


def sync():
    """
    Stage any local changes that exist.
    Commit them with the current time.
    Check if local is behind remote.
        If local is ahead of remote, push changes.
        If local is behind remote, pull and rebase changes.

    Does nothing at all while a pause lease is held, or while a rebase, merge,
    cherry-pick or revert is in progress, because staging then would commit
    conflict markers.
    """

    leases = active_leases()
    if leases:
        deadline, owner = leases[0]
        remaining = int(deadline - time.time())
        report_blocked(
            "paused",
            f"Paused, {remaining}s remaining (held by {owner}). "
            f"Run `syncshot.py resume` to sync now.",
        )
        return

    operation = in_progress_operation()
    if operation is not None:
        report_blocked(
            f"blocked by a {operation}",
            f"A {operation} is in progress, so syncing is on hold. Staging now "
            "would commit conflict markers. Resolve it by hand and syncshot "
            "will pick up again on its own.",
            level=logging.ERROR,
        )
        return

    report_unblocked()

    while True:
        changes = local_changes()
        if not changes:
            break
        stage_local_changes()
        commit_local_changes(len(changes))

    remote = remote_status()
    if remote < 0:  # Local is ahead.
        push()
    elif remote > 0:  # Remote is ahead.
        pull()
    else:
        logging.debug("In sync")


def report_blocked(reason, message, level=logging.INFO):
    """
    Say why syncing is on hold, but only when the reason changes.

    A pause or a stuck rebase lasts many periods. Logging it every time would
    bury the thing that caused it under identical lines, which is the noise this
    is meant to avoid, so repeats drop to debug.
    """

    global blocked_by

    if blocked_by == reason:
        logging.debug(message)
    else:
        logging.log(level, message)
        blocked_by = reason


def report_unblocked():
    """Note that whatever was holding syncing up has cleared."""

    global blocked_by

    if blocked_by is not None:
        logging.info(f"No longer {blocked_by}; syncing again")
        blocked_by = None


def pause_dir():
    """Path to the lease directory, which lives in the git dir so that
    `git add .` can never stage it."""

    result = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip()) / PAUSE_DIR_NAME


def parse_duration(text):
    """Turn "20s", "5m", "1h" or a bare number of seconds into an int."""

    match = re.fullmatch(r"(\d+)([smh]?)", text.strip())
    if not match:
        raise ValueError(f"Could not read '{text}' as a duration. Try 20s, 5m or 1h.")

    amount = int(match.group(1))
    multiplier = {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]
    seconds = amount * multiplier
    if seconds <= 0:
        raise ValueError("Duration must be greater than zero.")

    return seconds


def resolve_owner(owner=None):
    """Who holds a lease: an explicit name, else $SYNCSHOT_OWNER, else the user."""

    name = owner or os.environ.get("SYNCSHOT_OWNER") or getpass.getuser()

    return re.sub(r"[^A-Za-z0-9_-]", "-", name)


def active_leases():
    """
    Return [(deadline, owner)] for unexpired leases, soonest deadline first,
    deleting any that have expired on the way past.
    """

    directory = pause_dir()
    if not directory.is_dir():
        return []

    now = time.time()
    leases = []
    for entry in directory.iterdir():
        deadline_text, _, owner = entry.name.partition(".")
        try:
            deadline = int(deadline_text)
        except ValueError:
            logging.debug(f"Ignoring unrecognised file in pause dir: {entry.name}")
            continue

        if deadline <= now:
            logging.debug(f"Sweeping expired lease {entry.name}")
            entry.unlink(missing_ok=True)
        else:
            leases.append((deadline, owner))

    return sorted(leases)


def acquire_lease(seconds, owner=None):
    """Take a pause lease, replacing any this owner already holds.

    Also serves as the renewal for `hold`: calling it again extends the deadline
    without ever leaving this owner holding nothing.
    """

    owner = resolve_owner(owner)
    if seconds > MAX_PAUSE_SECONDS:
        print(f"Capping pause at {MAX_PAUSE_SECONDS}s (asked for {seconds}s).")
        seconds = MAX_PAUSE_SECONDS

    directory = pause_dir()
    directory.mkdir(exist_ok=True)

    # Write the new lease before dropping the old one. Doing it the other way
    # round leaves a window where this owner holds nothing, and a tick landing
    # in that window would sync mid-change. This is what makes renewal safe.
    # Round the deadline up. Truncating would make the lease live up to a second
    # less than asked for, which eats the margin `hold` renews inside of.
    deadline = math.ceil(time.time()) + seconds
    lease = directory / f"{deadline}.{owner}"
    lease.touch()

    for entry in directory.iterdir():
        _, _, entry_owner = entry.name.partition(".")
        if entry_owner == owner and entry != lease:
            entry.unlink(missing_ok=True)

    return deadline, owner, seconds


def release_lease(owner=None, release_all=False, announce=True):
    """Drop this owner's leases, or everyone's with release_all."""

    directory = pause_dir()
    if not directory.is_dir():
        if announce:
            print("Not paused.")
        return 0

    owner = resolve_owner(owner)
    removed = 0
    for entry in directory.iterdir():
        _, _, entry_owner = entry.name.partition(".")
        if release_all or entry_owner == owner:
            entry.unlink(missing_ok=True)
            removed += 1

    if announce:
        if removed:
            print(f"Released {removed} lease(s). Syncing resumes on the next tick.")
        else:
            print(f"No leases held by {owner}. Use --all to release every holder.")

    return removed


def run_held(command, owner=None):
    """
    Run a command with syncing paused for exactly as long as it takes.

    The lease is short and renewed while the command runs, so the pause lasts as
    long as needed but a holder that dies stops blocking syncing almost at once.
    Renewal is why this is not subject to MAX_PAUSE_SECONDS: a process that keeps
    renewing is demonstrably alive, which is the thing the cap exists to check.
    """

    owner = resolve_owner(owner)
    acquire_lease(HOLD_LEASE_SECONDS, owner)
    renew_every = max(1, HOLD_LEASE_SECONDS // 3)
    print(f"Paused while running: {' '.join(command)}")

    process = None
    try:
        process = subprocess.Popen(command)
        last_renewal = time.time()
        while process.poll() is None:
            time.sleep(0.2)
            if time.time() - last_renewal >= renew_every:
                acquire_lease(HOLD_LEASE_SECONDS, owner)
                last_renewal = time.time()
                logging.debug(f"Renewed lease for {owner}")
    except KeyboardInterrupt:
        # The child is in this process group and got the interrupt too; wait for
        # it so the lease is not released while it is still writing files.
        logging.info("Interrupted, waiting for the command to stop")
        if process is not None:
            process.wait()
    except FileNotFoundError:
        logging.error(f"Could not run '{command[0]}': no such command")
        return 127
    finally:
        release_lease(owner, announce=False)

    returncode = process.returncode if process is not None else 1
    if returncode < 0:
        # Popen reports a signal death as -N; shells report it as 128+N, and the
        # exit status of this script is what a caller will actually see.
        returncode = 128 - returncode
        print(f"Resumed. Command was killed by signal {returncode - 128}.")
    else:
        print(f"Resumed. Command exited {returncode}.")

    return returncode


def print_status():
    """Report whether syncing is paused and by whom."""

    leases = active_leases()
    if not leases:
        print("Not paused.")
        return

    now = time.time()
    print(f"Paused by {len(leases)} lease(s):")
    for deadline, owner in leases:
        clock = datetime.fromtimestamp(deadline).strftime("%H:%M:%S")
        print(f"  {owner:<24} until {clock} ({int(deadline - now)}s remaining)")


def in_progress_operation():
    """
    Return the name of an in-progress git operation that can leave conflict
    markers in the working tree, or None if the repository is in a normal state.

    `git status --porcelain` reports a conflicted file as dirty, so without this
    check a failed rebase leads straight to `git add .` staging the markers and
    committing them.
    """

    result = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"],
        capture_output=True,
        text=True,
        check=True,
    )
    git_dir = Path(result.stdout.strip())

    states = [
        ("rebase", "rebase-merge"),
        ("rebase", "rebase-apply"),
        ("merge", "MERGE_HEAD"),
        ("cherry-pick", "CHERRY_PICK_HEAD"),
        ("revert", "REVERT_HEAD"),
    ]
    for name, entry in states:
        if (git_dir / entry).exists():
            logging.debug(f"Found an in-progress {name} at {git_dir / entry}")
            return name

    return None


def local_changes():
    """
    The paths git reports as changed, as porcelain lines. Empty means everything
    is staged and committed. The count is what the commit line reports.
    """

    logging.debug("Checking if local is dirty")
    result = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
    )
    logging.debug(f"Git status output: {result.stdout.strip()}")

    return result.stdout.strip().splitlines()


def stage_local_changes():
    """Stage everything."""

    logging.debug("Staging local changes")
    subprocess.run(["git", "add", "."], capture_output=True, check=True)
    logging.debug("Local changes staged")


def commit_local_changes(count=None):
    """Commit with timestamp as the message. A commit is worth reporting."""

    message = datetime.now(timezone.utc).isoformat()
    result = subprocess.run(
        ["git", "commit", "-m", message], capture_output=True, text=True, check=True
    )
    logging.debug(result.stdout.strip())

    if count is None:
        logging.info("Committed")
    else:
        logging.info(f"Committed {count} file{'' if count == 1 else 's'}")


def network_env():
    """
    The environment for a git call that reaches the remote. ssh only notices a
    dead peer if it is asked to, so keepalives turn a silent hang into an
    ordinary non-zero exit, which the loop already knows how to report and retry.
    Whatever is already in GIT_SSH_COMMAND is kept and extended, not replaced.
    """

    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = f"{env.get('GIT_SSH_COMMAND', 'ssh')} {SSH_KEEPALIVE}"
    return env


def run_network_git(args, capture_output=False):
    """
    Run a git command that reaches the remote, under a deadline, and make sure it
    is really gone if the deadline has to be enforced. Killing the timed out
    command only kills git itself, which leaves the ssh it spawned holding the
    dead socket and one stray process behind per attempt, so the command gets its
    own session and the whole group is killed together.

    Raises the same exceptions a checked subprocess.run would, so callers and the
    sync loop see no difference between this and any other git call.
    """

    pipe = subprocess.PIPE if capture_output else None
    process = subprocess.Popen(
        args,
        stdout=pipe,
        stderr=pipe,
        text=True,
        env=network_env(),
        start_new_session=True,
    )

    try:
        stdout, stderr = process.communicate(timeout=NETWORK_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            args, NETWORK_TIMEOUT_SECONDS, output=stdout, stderr=stderr
        )

    if process.returncode != 0:
        raise subprocess.CalledProcessError(
            process.returncode, args, output=stdout, stderr=stderr
        )

    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def remote_status():
    """Comprare local branch to remote branch to see if local is ahead or behind."""

    logging.debug("Checking remote status")
    run_network_git(["git", "fetch"])
    result = subprocess.run(
        ["git", "status", "-b", "--porcelain=v1"],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = result.stdout.strip().split("\n")
    if not lines:
        return 0
    branch_line = lines[0]

    # Look for [behind N], [ahead N], or [ahead N, behind M] patterns
    if "[behind" in branch_line:
        # Extract behind count: "## main...origin/main [behind 3]"
        match = re.search(r"\[behind (\d+)", branch_line)
        if match:
            logging.debug(f"Local is behind by {match.group(1)} commits")
            return int(match.group(1))

    elif "[ahead" in branch_line and "behind" not in branch_line:
        # Extract ahead count: "## main...origin/main [ahead 2]"
        match = re.search(r"\[ahead (\d+)", branch_line)
        if match:
            logging.debug(f"Local is ahead by {match.group(1)} commits")
            return -int(match.group(1))

    elif "[ahead" in branch_line and "behind" in branch_line:
        # Handle diverged case: "## main...origin/main [ahead 1, behind 2]"
        behind_match = re.search(r"behind (\d+)", branch_line)
        ahead_match = re.search(r"ahead (\d+)", branch_line)
        if behind_match:
            ahead = ahead_match.group(1) if ahead_match else "?"
            logging.debug(
                f"Local has diverged: ahead by {ahead}, behind by {behind_match.group(1)} commits"
            )
            return int(behind_match.group(1))

    # No ahead/behind info means in sync
    logging.debug("Local is in sync with remote")
    return 0


def push():
    """Push local changes to remote."""

    logging.debug("Pushing to remote")
    run_network_git(["git", "push"], capture_output=True)
    logging.info("Pushed to remote")


def pull():
    """Pull changes from remote and rebase."""

    logging.debug("Pulling from remote")
    result = run_network_git(["git", "pull", "--rebase=True"], capture_output=True)
    logging.debug(result.stdout.strip())
    logging.info("Pulled and rebased onto remote")


def build_parser():
    """
    Bare `syncshot.py` still runs the daemon, so the subcommand is optional and
    `run` is only an explicit alias for it.
    """

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--period",
        type=int,
        default=argparse.SUPPRESS,
        help="Time in seconds between sync attempts (default: 10)",
    )
    common.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Enable debug logging",
    )

    parser = argparse.ArgumentParser(
        description="Syncshot: Keep your git repository in sync.",
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "run", parents=[common], help="Run the sync loop (the default)"
    )

    pause = subparsers.add_parser(
        "pause", help="Hold off syncing while you make a big change"
    )
    pause.add_argument(
        "duration",
        nargs="?",
        default=f"{DEFAULT_PAUSE_SECONDS}s",
        help=f"How long to pause: 20s, 5m, 1h (default: {DEFAULT_PAUSE_SECONDS}s)",
    )
    pause.add_argument("--owner", help="Name this lease (default: $SYNCSHOT_OWNER)")

    resume = subparsers.add_parser("resume", help="Release your pause and sync again")
    resume.add_argument("--owner", help="Whose lease to release")
    resume.add_argument(
        "--all", action="store_true", help="Release every holder's lease, not just yours"
    )

    hold = subparsers.add_parser(
        "hold", help="Run a command with syncing paused for exactly as long as it takes"
    )
    hold.add_argument("--owner", help="Name this lease (default: $SYNCSHOT_OWNER)")
    # Not "command": that is the subparser's own dest, and a positional of the
    # same name silently overwrites which subcommand was chosen.
    hold.add_argument(
        "argv",
        metavar="command",
        nargs=argparse.REMAINDER,
        help="-- followed by the command to run",
    )

    subparsers.add_parser("status", help="Show whether syncing is paused, and by whom")

    return parser


if __name__ == "__main__":
    """
    Main entry point for the script.
    Processes arguments and starts the sync process by calling `main`.
    """

    args = build_parser().parse_args()
    period = getattr(args, "period", 10)
    command = getattr(args, "command", None)

    # The daemon's output is read later, out of a file, so its lines carry a
    # timestamp. The one-shot subcommands are read as they are typed and do not.
    daemon = command in (None, "run")
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "debug", False) else logging.INFO,
        format=(
            "%(asctime)s %(levelname)-7s %(message)s" if daemon else "%(levelname)s: %(message)s"
        ),
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if command == "pause":
        try:
            seconds = parse_duration(args.duration)
        except ValueError as e:
            logging.error(e)
            exit(1)
        deadline, owner, seconds = acquire_lease(seconds, args.owner)
        clock = datetime.fromtimestamp(deadline).strftime("%H:%M:%S")
        print(f"Paused for {seconds}s, until {clock} (held by {owner}).")
    elif command == "resume":
        release_lease(args.owner, release_all=args.all)
    elif command == "hold":
        argv = args.argv
        if argv and argv[0] == "--":
            argv = argv[1:]
        if not argv:
            logging.error("Nothing to run. Try: syncshot.py hold -- git mv a b")
            exit(1)
        exit(run_held(argv, args.owner))
    elif command == "status":
        print_status()
    else:
        if period <= 0:
            logging.error("Period must be a positive integer")
            exit(1)
        main(period)
