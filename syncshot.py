import subprocess
import logging
import re
import os
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


def main(period):
    setup_signal_handlers()
    logging.info("Syncshot is running")

    while not shutdown_requested:
        logging.info("Syncing...")
        try:
            sync()
        except subprocess.CalledProcessError as e:
            logging.error(f"An error occurred while syncing: {e}")
            logging.debug(f"Command output: {e.output}")
            logging.debug(f"Command stderr: {e.stderr}")
            logging.debug("Continuing to next sync attempt")

        if shutdown_requested:
            break

        logging.info("Done")
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
        logging.info(
            f"Paused, {remaining}s remaining (held by {owner}). "
            f"Run `syncshot.py resume` to sync now."
        )
        return

    operation = in_progress_operation()
    if operation is not None:
        logging.error(
            f"A {operation} is in progress, so this sync was skipped. "
            "Staging now would commit conflict markers. Resolve it by hand "
            "and syncshot will pick up again on its own."
        )
        return

    while is_local_dirty():
        stage_local_changes()
        commit_local_changes()

    remote = remote_status()
    if remote < 0:  # Local is ahead.
        push()
    elif remote > 0:  # Remote is ahead.
        pull()
    else:
        logging.debug("In sync")


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
    """Take a pause lease, replacing any this owner already holds."""

    owner = resolve_owner(owner)
    if seconds > MAX_PAUSE_SECONDS:
        print(f"Capping pause at {MAX_PAUSE_SECONDS}s (asked for {seconds}s).")
        seconds = MAX_PAUSE_SECONDS

    directory = pause_dir()
    directory.mkdir(exist_ok=True)
    release_lease(owner, announce=False)

    deadline = int(time.time()) + seconds
    (directory / f"{deadline}.{owner}").touch()

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


def is_local_dirty():
    """
    This will return True if git status has unstaged changes.
    It will return False if all changes have been staged and committed.
    """

    logging.debug("Checking if local is dirty")
    result = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
    )
    logging.debug(f"Git status output: {result.stdout.strip()}")

    return bool(result.stdout.strip())


def stage_local_changes():
    """Stage everything."""

    logging.debug("Staging local changes")
    subprocess.run(["git", "add", "."], capture_output=False, check=True)
    logging.debug("Local changes staged")


def commit_local_changes():
    """Commit with timestamp as the message."""

    logging.debug("Committing local changes")
    message = datetime.now(timezone.utc).isoformat()
    subprocess.run(["git", "commit", "-m", message], capture_output=False, check=True)
    logging.debug("Local changes committed")


def remote_status():
    """Comprare local branch to remote branch to see if local is ahead or behind."""

    logging.debug("Checking remote status")
    subprocess.run(["git", "fetch"], check=True)
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
    subprocess.run(["git", "push"], capture_output=True, check=True)
    logging.debug("Push completed")


def pull():
    """Pull changes from remote and rebase."""

    logging.debug("Pulling from remote")
    subprocess.run(["git", "pull", "--rebase=True"], check=True)
    logging.debug("Pull completed")


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

    subparsers.add_parser("status", help="Show whether syncing is paused, and by whom")

    return parser


if __name__ == "__main__":
    """
    Main entry point for the script.
    Processes arguments and starts the sync process by calling `main`.
    """

    args = build_parser().parse_args()
    period = getattr(args, "period", 10)
    if getattr(args, "debug", False):
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    command = getattr(args, "command", None)

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
    elif command == "status":
        print_status()
    else:
        if period <= 0:
            logging.error("Period must be a positive integer")
            exit(1)
        main(period)
