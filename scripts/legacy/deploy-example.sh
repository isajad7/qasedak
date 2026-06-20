#!/usr/bin/env bash
set -Eeuo pipefail

cat <<'EOF'
This generic legacy deploy example is intentionally non-operational.

Use scripts/install.sh for supported installs:

  scripts/install.sh --dry-run --yes --install-dir /opt/qasedak

For remote deployment, create a private operator script outside the public
repository and keep production hosts, paths, service names, proxy credentials,
bot tokens, and card data out of git.
EOF
