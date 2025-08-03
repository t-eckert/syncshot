import subprocess
import logging
import re
import time
import argparse
import signal
import sys

from datetime import datetime, timezone

# Global flag for graceful shutdown
shutdown_requested = False

def signal_handler(signum, frame):
    """Handle interrupt signals gracefully"""
    global shutdown_requested
    signal_name = signal.Signals(signum).name
    logging.info(f"Received {signal_name}. Finishing current sync and shutting down gracefully...")
    shutdown_requested = True

def setup_signal_handlers():
    """Set up signal handlers for graceful shutdown"""
    signal.signal(signal.SIGINT, signal_handler)   # CTRL+C
    signal.signal(signal.SIGTERM, signal_handler)  # Termination signal
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, signal_handler)   # Hangup signal

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
        
        # Sleep in smaller increments to check for shutdown more frequently
        sleep_remaining = period
        while sleep_remaining > 0 and not shutdown_requested:
            sleep_time = min(1, sleep_remaining)  # Sleep in 1-second increments
            time.sleep(sleep_time)
            sleep_remaining -= sleep_time
    
    logging.info("Syncshot shutting down gracefully")


def sync():
    """
    Stage any local changes that exist.
    Commit them with the current time.
    Check if local is behind remote.
        If local is ahead of remote, push changes.
        If local is behind remote, pull and rebase changes.
    """

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
    """
    Stage everything.
    """

    logging.debug("Staging local changes")
    subprocess.run(["git", "add", "."], capture_output=False, check=True)
    logging.debug("Local changes staged")


def commit_local_changes():
    """
    Commit with timestamp as the message.
    """

    logging.debug("Committing local changes")
    message = datetime.now(timezone.utc).isoformat()
    subprocess.run(["git", "commit", "-m", message], capture_output=False, check=True)
    logging.debug("Local changes committed")


def remote_status():
    """
    Comprare local branch to remote branch to see if local is ahead or behind.
    """

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
        if behind_match:
            logging.debug(f"Local is ahead by {behind_match.group(1)} commits")
            return int(behind_match.group(1))

    # No ahead/behind info means in sync
    logging.debug("Local is in sync with remote")
    return 0


def push():
    """
    Push local changes to remote.
    """

    logging.debug("Pushing to remote")
    subprocess.run(["git", "push"], capture_output=True, check=True)
    logging.debug("Push completed")


def pull():
    """
    Pull changes from remote and rebase.
    """

    logging.debug("Pulling from remote")
    subprocess.run(["git", "pull", "--rebase=True"])
    logging.debug("Pull completed")


if __name__ == "__main__":
    """
    Main entry point for the script.
    Processes arguments and starts the sync process by calling `main`.
    """

    parser = argparse.ArgumentParser(
        description="Syncshot: Keep your git repository in sync."
    )
    parser.add_argument(
        "--period",
        type=int,
        default=10,
        help="Time in seconds between sync attempts (default: 10)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()
    period = args.period
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
    if period <= 0:
        logging.error("Period must be a positive integer")
        exit(1)

    main(period)
