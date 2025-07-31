import subprocess


def main():
    local = is_local_dirty()
    print("Is local dirty: ", local)

    print("Syncshot")


def is_local_dirty() -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
    )

    return bool(result.stdout.strip())


if __name__ == "__main__":
    main()
