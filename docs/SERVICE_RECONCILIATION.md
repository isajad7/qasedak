# Service Reconciliation

Service reconciliation compares local `VPNClient` rows with the exact X-UI scope they belong to:

```text
panel + node + inbound + client identity
```

It is intentionally read-only on X-UI. It never deletes, disables, enables, or edits a remote X-UI client.

## Status Meanings

- `not_checked`: no saved panel check exists.
- `remote_active`: the client exists in the exact panel/node/inbound scope and is enabled or has no explicit disabled flag.
- `remote_disabled`: the client exists in the exact scope but the panel reports it disabled.
- `remote_missing`: a complete, successful inbound client list was read and no exact match was found.
- `panel_unreachable`: timeout, proxy error, login/auth failure, or network failure. This is not deletion proof.
- `inbound_missing`: the panel answered successfully but the inbound itself was not found.
- `ambiguous`: more than one remote match or unsafe local scope. This must never be cleaned up automatically.
- `unknown`: local scope or panel response was incomplete.

Timeouts, proxy failures, node offline errors, and login failures are never treated as `remote_missing`.

## Admin Workflow

Open:

```text
/admin/store/services/workbench/
```

Use **بررسی سرویس‌ها با پنل** to run a POST-only, CSRF-protected check. GET page loads never call Telegram or X-UI.

After a check, the workbench shows local status, latest remote status, checked time, summary cards, and filters for active/disabled/missing/problem/not-checked results.

Use **پاک‌سازی سرویس‌های حذف‌شده** only when fresh cleanup candidates exist. Each candidate is revalidated read-only before any local mutation.

## Soft Delete Semantics

Cleanup only soft-deletes the local `VPNClient` after revalidation still proves `remote_missing` in the same scope.

Local soft-delete:

- sets the VPN client to deleted semantics already used by Qasedak
- clears active config links from user-facing delivery
- preserves `Order`, `Customer`, payment/receipt data, and audit history
- writes `VPNClientActionLog` with `admin_soft_delete_remote_missing`

It does not call any X-UI delete API.

## Multi-Node Safety

Matching is scoped by panel, node, inbound, and client identity. Same email on another node or inbound is not a match. `remote_client_key`, UUID/client credential, and email/config identifiers are only considered inside the exact scope.

Stored check scope contains safe metadata only: panel id, local inbound pk, X-UI inbound id, node id, and inbound remote scope key. Full UUIDs, config links, tokens, passwords, proxy URLs, phone numbers, and complete emails must not be displayed or persisted in reconciliation results.

## Command

Preview all non-deleted clients without local mutation:

```bash
./venv/bin/python manage.py reconcile_vpn_clients --all --dry-run --verbose
```

Persist latest check fields:

```bash
./venv/bin/python manage.py reconcile_vpn_clients --all --apply-check-results --verbose
```

Soft-delete still-confirmed missing clients:

```bash
./venv/bin/python manage.py reconcile_vpn_clients --all --apply-check-results --soft-delete-confirmed-missing --confirm SOFT_DELETE_REMOTE_MISSING
```

Useful limits:

```bash
./venv/bin/python manage.py reconcile_vpn_clients --panel-id 1 --limit 100 --dry-run
./venv/bin/python manage.py reconcile_vpn_clients --client-id 123 --apply-check-results
```

The command is not auto-enabled by timers.

## Recovery

Because cleanup is local soft-delete, orders, customers, payments, and audit records remain. To recover a mistakenly hidden service, inspect the `VPNClientActionLog`, confirm the remote state manually, then restore the local `VPNClient` fields through a controlled admin/data-fix process. Do not recreate orders or payments.
