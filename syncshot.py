import subprocess
import logging
import re
import time

from datetime import datetime, timezone


def main(period=10):
    logging.info("Syncshot is running")
    while True:
        logging.info("Syncing...")
        sync()
        logging.info("Done")
        logging.debug(f"Sleeping {period}")
        time.sleep(period)


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
    It will return False if all changes have been staged.
    """

    result = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
    )

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
    Commit with timestamp
    """

    logging.debug("Committing local changes")
    message = generate_commit_message()
    subprocess.run(["git", "commit", "-m", message], capture_output=False, check=True)
    logging.debug("Local changes committed")


def generate_commit_message():
    iso_now = datetime.now(timezone.utc).isoformat()
    return iso_now


def remote_status():
    subprocess.run(["git", "fetch"])
    current_branch = subprocess.check_output(
        ["git", "branch", "--show-current"], text=True
    ).strip()

    logging.debug(f"Current branch:{current_branch}")
    print(current_branch)

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
            return int(match.group(1))

    elif "[ahead" in branch_line and "behind" not in branch_line:
        # Extract ahead count: "## main...origin/main [ahead 2]"
        match = re.search(r"\[ahead (\d+)", branch_line)
        if match:
            return -int(match.group(1))

    elif "[ahead" in branch_line and "behind" in branch_line:
        # Handle diverged case: "## main...origin/main [ahead 1, behind 2]"
        behind_match = re.search(r"behind (\d+)", branch_line)
        if behind_match:
            return int(behind_match.group(1))

    # No ahead/behind info means in sync
    return 0


def push():
    logging.debug("Pushing!")
    subprocess.run(["git", "push"], capture_output=True, check=True)


def pull():
    logging.debug("Pulling")
    subprocess.run(["git", "pull", "--rebase=True"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    main()
