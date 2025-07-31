import subprocess
import logging

from datetime import datetime, timezone


def main():
    logging.info("Syncshot is running")

    while is_local_dirty():
        stage_local_changes()
        commit_local_changes()


def is_local_dirty() -> bool:
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    main()
