# Update

For the default install path:

```bash
sudo bash -c 'set -e; apt-get update; apt-get install -y git; tmp=$(mktemp -d); git clone https://github.com/isajad7/qasedak.git "$tmp/qasedak"; bash "$tmp/qasedak/scripts/update.sh" --install-dir /opt/qasedak --source-dir "$tmp/qasedak" --yes --restart'
```

If you installed somewhere else, change `/opt/qasedak`.

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
sudo bash -c 'set -e; apt-get update; apt-get install -y git; tmp=$(mktemp -d); git clone https://github.com/isajad7/qasedak.git "$tmp/qasedak"; bash "$tmp/qasedak/scripts/update.sh" --install-dir /opt/qasedak --source-dir "$tmp/qasedak" --dry-run --yes'
```

## Manual Backup

```bash
sudo /opt/qasedak/scripts/backup.sh --install-dir /opt/qasedak --output-dir /opt/qasedak/backups --yes
```
