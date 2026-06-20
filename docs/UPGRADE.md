# Update

For the default install path:

```bash
curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/update_from_github.sh | sudo bash
```

If you installed somewhere else:

```bash
curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/update_from_github.sh | sudo env QASEDAK_INSTALL_DIR=/your/path bash
```

The update command:

- creates a backup first
- syncs the latest code
- installs dependencies
- runs Django checks and migrations
- collects static files
- restarts services when they exist
- runs doctor after update

## Dry Run

```bash
curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/update_from_github.sh | sudo bash -s -- --dry-run --no-restart
```

## Manual Backup

```bash
sudo /opt/qasedak/scripts/backup.sh --install-dir /opt/qasedak --output-dir /opt/qasedak/backups --yes
```
