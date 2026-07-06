# 3X-UI Multi-Node Operations

Qasedak supports legacy Sanaei/X-UI panels and has a conservative compatibility layer for modern 3X-UI node-aware panels.

## Terms

- Panel: the Sanaei/3X-UI web panel configured in Django Admin.
- Node: a remote 3X-UI node reported by modern 3.x APIs.
- Inbound scope: `(panel, xui_node_id, inbound_id)`. The same `inbound_id` can exist more than once when node scope differs.
- Remote client key: local tracking key made from panel, node, inbound, and remote client identity.
- Managed host: v3.4.0 host/share address data used when generating links.

## Setup Flow

1. Add or keep the panel in Django Admin.
2. Run a local audit:

```bash
./venv/bin/python manage.py audit_xui_compatibility --panel-id <id> --no-write
```

3. Run a live read-only audit when network access is intended:

```bash
./venv/bin/python manage.py audit_xui_compatibility --panel-id <id> --live --no-write --verbose
```

4. If the result is expected, persist metadata:

```bash
./venv/bin/python manage.py audit_xui_compatibility --panel-id <id> --live --write
```

5. Preview topology sync:

```bash
./venv/bin/python manage.py sync_xui_topology --panel-id <id> --dry-run --verbose
```

6. Apply only after checking the preview:

```bash
./venv/bin/python manage.py sync_xui_topology --panel-id <id> --apply --confirm APPLY_XUI_TOPOLOGY_SYNC
```

## Sales Routing

Legacy paid orders may keep the historical pre-create-inactive workflow: Qasedak creates a disabled remote client during checkout and enables it only after payment approval.

Modern paid orders use deferred paid provisioning because official modern 3X-UI client creation does not safely preserve disabled creation. For detected `modern_single_node` and `modern_multi_node` panels:

- Checkout creates only the local `Order`.
- The exact `(panel, node, inbound)` scope is frozen on the order.
- No remote 3X-UI client or config link is created before payment approval.
- After approval, Qasedak creates an enabled client on the frozen scope.
- The order is completed only after remote lookup verifies that the client exists and is enabled.

`unknown_safe` panels still fail closed for destructive operations. Run compatibility audit before routing sales to them.

Free trials create enabled, short-lived clients directly and can use the modern create endpoint when the panel profile is known. Admin direct grants/purchases on modern panels also create enabled clients directly because the admin action is explicit and has no pending customer payment window.

Renewals never move a client to a new route. They update the original client on its existing panel/node/inbound scope.

## Paid Order Lifecycle

Modern paid order:

1. Customer submits checkout/payment proof.
2. `Order.provisioning_status = pending`; `Order.metadata.provisioning_scope` stores panel, node, and X-UI inbound IDs.
3. Approval calls `approve_and_provision_order(...)`.
4. The order row is locked and a deterministic UUID/email/subId is derived from the order and scope.
5. Qasedak looks up the exact remote scope. If the client already exists and is enabled, it is reused.
6. If no remote client exists, Qasedak creates one enabled and verifies it by reading the same inbound.
7. Only after verification does Qasedak create/update the local `VPNClient`, mark the order completed, and trigger delivery.

Failure behavior:

- Remote create/verify failure leaves the order `confirmed` with `provisioning_status = failed`.
- No active local `VPNClient` is created.
- No config delivery is attempted.
- Retry is safe because the same deterministic identity is reused.

Run reconciliation in dry-run mode:

```bash
./venv/bin/python manage.py reconcile_order_provisioning --order-id <id> --dry-run --verbose
```

Retry provisioning only after review:

```bash
./venv/bin/python manage.py reconcile_order_provisioning --order-id <id> --apply --verbose
```

Use `--all-failed --limit <n>` to review batches. Dry-run is the default; `--apply` may create enabled remote clients.

## Usage

Panel usage collection reads inbound/client stats and dedupes repeated client identifiers before totals are added. If an inbound or node cannot be read, the snapshot is partial or failed. It is never converted to zero usage.

Run:

```bash
./venv/bin/python manage.py collect_panel_usage_snapshots --dry-run --panel-id <id> --verbose
```

## Health

Health checks log in, read inbounds, and run read-only compatibility discovery. Node issues are stored in health metadata and produce a warning.

Run:

```bash
./venv/bin/python manage.py check_panel_health --dry-run --panel-id <id> --verbose
```

Admin GET pages show saved health and compatibility metadata only. They do not call X-UI.

## Links

Link generation prefers managed host data when available, then `shareAddrStrategy`/`shareAddr`, then local inbound settings. Full generated links are treated as secrets and must not be printed in logs, fixtures, or reports.

## Mutation Safety

Allowed:

- Legacy inbound-scoped create/update/delete/reset through the old API.
- Modern create-enabled through `/panel/api/clients/add`.
- Modern update through `/panel/api/clients/update/{email}?inboundIds=<id>`.

Guarded:

- Modern delete/reset are email-wide. They are allowed only after verifying the client has one attachment, unless a caller explicitly opts into multi-scope behavior.

Blocked:

- Modern create-inactive.
- Unknown profile destructive operations.
- Ambiguous local inbound/client scope.

## Troubleshooting

- `unknown_safe`: run live audit and review endpoint errors.
- Duplicate `inbound_id`: run topology sync dry-run and ensure each local inbound has the correct `xui_node_id`.
- Partial usage: inspect `PanelUsageSnapshot.metadata.inbound_errors`; do not treat missing node data as zero.
- Failed provisioning on modern 3.x: run `reconcile_order_provisioning --order-id <id> --dry-run --verbose`; apply only after confirming the frozen scope is correct.
- Rollback note: do not delete a remote client unless the scope and identity are exact. If DB commit failed after remote create, reconciliation should reuse the existing deterministic remote client.
