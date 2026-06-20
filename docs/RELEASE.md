# Release Readiness

Use this checklist before publishing a private or public GitHub release. Do not deploy from this process.

## Pre-Release Checklist

- Confirm the working tree contains no tracked runtime artifacts: `.env`, `data/`, `db.sqlite3`, `media/`, `venv/`, `node_modules/`, `backups/`, `logs/`, `static_root/`, archives, `__pycache__/`, or `*.pyc`.
- Run the release checker:

```bash
scripts/release_check.sh --quick
scripts/release_check.sh --full
```

- Confirm CI is green on the release branch.
- Confirm public docs use placeholders such as `example.com`, `203.0.113.10`, `/opt/vpn-store`, `vpn-store-web.service`, and `vpn-store-telegram.service`.
- Confirm no production domain, IP, path, personal deploy name, proxy credential, bot token, or card number appears in public source or docs.
- Choose a license before public release. See `docs/productization/06-license-decision.md`.
- Keep any private audit notes, real environment files, install configs, databases, media, logs, and backups out of the public repository.

## Install Command Placeholder

Use this only after the repository exists and `OWNER/REPO` is replaced intentionally:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/OWNER/REPO/main/scripts/install.sh)
```

The supported install path today is still a local repository checkout:

```bash
git clone https://github.com/OWNER/REPO.git
cd REPO
cp .env.example .env
scripts/install.sh --dry-run --yes --install-dir /opt/vpn-store
```

## Release Package

Dry-run release packages should use a timestamped name such as
`dist/vpn-store-productized-YYYYMMDD-HHMM.tar.gz`.

Include source code, scripts, docs, migrations, templates, static source files,
`.env.example`, and `docs/productization/install.config.example.json`.
Exclude `.git`, `.env`, databases, `data/`, `media/`, `venv/`, `node_modules/`,
`backups/`, `logs/`, `private_imports/`, Python caches, and archive artifacts.

## Tag and Release Process

1. Run `scripts/release_check.sh --full`.
2. Push the release branch and wait for GitHub Actions CI to pass.
3. Confirm license status is resolved for public release.
4. Create an annotated tag such as `v0.1.0`.
5. Draft release notes that include install status, known limitations, migration notes, and rollback notes.
6. Publish first as private/internal if license or operator-specific docs are still pending.

## Post-Release Smoke Test

On a clean test host or disposable VM:

```bash
git clone https://github.com/OWNER/REPO.git
cd REPO
scripts/install.sh --dry-run --yes --install-dir /opt/vpn-store
```

For a real private smoke test, run the installer with a private config, then run:

```bash
/opt/vpn-store/scripts/doctor.sh --install-dir /opt/vpn-store --no-fail
```

Live Telegram and X-UI checks stay opt-in.

## Rollback Note

If a release is found unsafe after publication, mark it as withdrawn, delete or supersede the tag as appropriate for the repository policy, and publish a note that points users back to the previous known-good tag. Do not publish backup archives, `.env`, install configs with secrets, or runtime databases as release assets.
