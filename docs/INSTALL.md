# Install

Use one command on a fresh Ubuntu/Debian server:

```bash
curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/install_from_github.sh | sudo bash
```

The installer asks for the needed basics:

- install directory
- optional domain
- server public IP
- TLS when a domain is available
- admin username/email/password
- database engine (`postgres` by default, `sqlite` optional)
- PostgreSQL database/user details, or SQLite database path
- systemd
- nginx
- non-live doctor check

It also installs Python 3.12/venv if the server Python is older. In PostgreSQL mode it installs `postgresql`, `postgresql-client`, and `libpq-dev`, generates a database password, stores it only in `.env`, creates the role/database idempotently, and redacts the password from the summary. SQLite remains supported for development, tests, small installs, and fallback.
If an old/partial install exists, it warns before doing anything destructive.
At the end, it prints the admin panel URL, username, and password.

For SaaS hosts that run tenant containers, host PostgreSQL must be reachable from Docker bridge networking. PostgreSQL must listen on both `127.0.0.1:5432` and `172.17.0.1:5432`, and `pg_hba.conf` must include:

```text
host all all 172.17.0.0/16 scram-sha-256
```

This does not expose PostgreSQL publicly. It allows tenant containers on the local Docker bridge to connect to the host database through `172.17.0.1`. To let the installer configure that host-level PostgreSQL access, opt in explicitly:

```bash
sudo /opt/qasedak/scripts/install.sh --configure-postgres-docker-bridge
```

The opt-in path backs up `postgresql.conf` and `pg_hba.conf`, sets `listen_addresses = '127.0.0.1,172.17.0.1'`, appends the Docker bridge HBA rule if missing, and restarts PostgreSQL. It must not be changed to `*` or a public interface.

Default install directory:

```text
/opt/qasedak
```

After install, open Django Admin and start from the responsive Qasedak admin home dashboard:

```text
/admin/
```

The detailed owner dashboard is also available at:

```text
/admin/store/dashboard/
```

The dashboards show the overall state and action items from DB/log data only. They do not replace `doctor.sh`.

Use the Setup Center to complete the installation:

```text
/admin/store/setup/
```

Before importing production data or moving servers, open the Backup & Restore Center:

```text
/admin/store/backups/
```

Create backups there, upload migration packages there, validate restore compatibility there, and run the generated restore command over SSH. Admin validation is safe; destructive restore apply is not performed inside a web request.

For the shortest owner-facing setup path, use the guided wizard:

```text
/admin/store/setup/wizard/
```

Telegram, X-UI/Sanaei, plans, routes, payment details, and Revenue Engine rollout are completed from Django Admin. The wizard and Setup Center do not run live Telegram/X-UI checks automatically. The installer is intentionally minimal; missing integration records after install are setup warnings, not installer failures. Keep Revenue Engine dry-run at first and review logs before real sends.

Full guide: [Post-Install Setup](POST_INSTALL_SETUP.md).

## Advanced Install

Only use this if you want Telegram/X-UI/Plan/Payment questions during install:

```bash
curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/install_from_github.sh | sudo bash -s -- --advanced
```

## Existing Config

```bash
curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/install_from_github.sh | sudo bash -s -- --config /root/install.config.json
```

The public config example uses `database.engine=postgres` with `database.postgres.password_env` instead of a raw password. Put real secrets in the runtime environment or let the installer generate them into `.env`.

## Doctor

```bash
sudo /opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --no-fail
```

In PostgreSQL mode, doctor also checks the SaaS Docker bridge requirement: `ss` must show PostgreSQL listening on `172.17.0.1:5432`, `pg_hba.conf` must allow `172.17.0.0/16` with `scram-sha-256`, and a throwaway Docker container must be able to run `pg_isready` against `172.17.0.1:5432`. Doctor output redacts secrets and does not print `DATABASE_URL` or DB passwords.

Live Telegram/X-UI checks are optional:

```bash
sudo /opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --live-bot --live-xui --no-fail
```

## Delete

```bash
curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/uninstall_from_github.sh | sudo bash
```
