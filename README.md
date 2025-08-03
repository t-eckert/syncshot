# Syncshot

Keep your git repository in sync.

Syncshot allows you to use a remote Git repo for simple syncing. It's useful for backing up folders of markdown, CSV data, and anything where backups are valuable, but you don't want to handle manually committing and pushing.

This repository is synced using the tool itself. The tool is complete. It runs using only the Python standard library and shells out to Git.

## Running Syncshot

Syncshot is a single file script, so you can just run it by grabbing the file from GitHub.

```sh
curl https://raw.githubusercontent.com/t-eckert/syncshot/refs/heads/main/syncshot.py | python3 
```
