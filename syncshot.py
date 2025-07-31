import subprocess
import logging
import re

from datetime import datetime, timezone


def main():
    logging.info("Syncshot is running")

    while is_local_dirty():
        stage_local_changes()
        commit_local_changes()

    remote = remote_status()
    logging.debug(remote)


def is_local_dirty():
    """
    This will return True if git status has unstaged changes.
    It will return False if all changes have been staged
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

    print(result.stdout)
    status = re.match("\\[*.\\]", result.stdout)
    print(status)

    return ("ahead", 3)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    main()
