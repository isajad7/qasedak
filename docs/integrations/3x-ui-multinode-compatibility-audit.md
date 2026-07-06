# 3X-UI Multi-Node Compatibility Audit

Date: 2026-06-23

This audit covers Qasedak/VPN Store compatibility with the official Sanaei 3X-UI project. It used only the upstream repository:

- `MHSanaei/3x-ui` v2.9.4: https://github.com/MHSanaei/3x-ui/tree/v2.9.4
- `MHSanaei/3x-ui` v3.3.1: https://github.com/MHSanaei/3x-ui/tree/v3.3.1
- `MHSanaei/3x-ui` v3.4.0: https://github.com/MHSanaei/3x-ui/tree/v3.4.0
- v3.4.0 release page: https://github.com/MHSanaei/3x-ui/releases/tag/v3.4.0

## Findings

Legacy v2.x exposes the old inbound-scoped client endpoints under `/panel/api/inbounds/*`, including add, update, delete, reset traffic, inbound detail, and online clients.

Modern v3.x exposes client-first endpoints under `/panel/api/clients/*`:

- `POST /panel/api/clients/add` with `{ "client": ..., "inboundIds": [...] }`
- `POST /panel/api/clients/update/{email}?inboundIds=...`
- `POST /panel/api/clients/del/{email}`
- `GET /panel/api/clients/traffic/{email}`
- `POST /panel/api/clients/onlines`
- `POST /panel/api/clients/resetTraffic/{email}`

Modern v3.x also adds topology APIs under `/panel/api/nodes/*`, managed hosts under `/panel/api/hosts/*` in v3.4.0, and inbound fields such as `nodeId`, `originNodeGuid`, `shareAddrStrategy`, and `shareAddr`.

The important incompatibility is disabled client creation. In v3.3.1 and v3.4.0, the official `ClientService.Create` path treats an omitted or false `enable` value as enabled. Qasedak's checkout workflow creates a disabled panel client before payment approval, then enables it after verification. Creating that client through the modern endpoint would briefly or permanently create an active service before payment. Qasedak therefore refuses create-inactive on detected modern profiles.

## Compatibility Decision

Status: `MODERN_PAID_DEFERRED_READY_LOCALLY`; destructive live smoke testing still requires an explicit test panel/client.

Supported now:

- Legacy v2.x single-node create-inactive, enable, renew, delete, usage, and link generation.
- Modern v3.x read-only discovery, health metadata, node-aware inbound scope, managed-host link generation, usage dedupe, create-enabled, inbound-filtered update, and deferred paid provisioning.
- Modern paid checkout stores only local order/scope state before payment approval. Approval creates an enabled client on the frozen scope, verifies it remotely, then completes the order and delivers config.
- Modern v3.x delete/reset only when the client is verified as attached to one inbound, or when a caller explicitly allows multi-scope behavior.

Blocked by design:

- Modern v3.x create-inactive for normal paid checkout.
- Unknown compatibility profile destructive operations.
- Ambiguous update/delete when the same inbound ID or client identifier appears in more than one local scope.
- Treating offline/unreadable node usage as zero.

## Local Changes

The public facade remains `store/xui_api.py`. Version and scope decisions are internal:

- `store/xui_compat/` contains DTOs, normalizers, capability detection, and safe-scope checks.
- `Panel` stores detected version/profile/metadata.
- `Inbound` stores `xui_node_id`, `xui_node_name`, `xui_source`, `xui_remote_key`, and node sync state.
- `VPNClient` stores `xui_node_id` and `remote_client_key`.
- Inbound uniqueness changed from `(panel, inbound_id)` to `(panel, xui_node_id, inbound_id)`.

## Commands

Read-only local audit:

```bash
./venv/bin/python manage.py audit_xui_compatibility --panel-id <id> --no-write
```

Read-only live audit:

```bash
./venv/bin/python manage.py audit_xui_compatibility --panel-id <id> --live --no-write --verbose
```

Persist detected local compatibility metadata:

```bash
./venv/bin/python manage.py audit_xui_compatibility --panel-id <id> --live --write
```

Preview topology sync:

```bash
./venv/bin/python manage.py sync_xui_topology --panel-id <id> --dry-run --verbose
```

Apply safe local topology metadata only after review:

```bash
./venv/bin/python manage.py sync_xui_topology --panel-id <id> --apply --confirm APPLY_XUI_TOPOLOGY_SYNC
```

Read-only smoke test:

```bash
./venv/bin/python manage.py test_xui_node_compatibility --panel-id <id> --confirm TEST_ONLY_XUI_NODE_COMPATIBILITY --verbose
```

Order provisioning reconciliation:

```bash
./venv/bin/python manage.py reconcile_order_provisioning --order-id <id> --dry-run --verbose
```

## Verification Rules

- Admin GET pages must not call X-UI.
- Audit/sync/test commands do not mutate remote clients.
- Reports, fixtures, and logs must not include full config links, full UUIDs, passwords, proxy credentials, or tokens.
- Unknown endpoints and unknown compatibility profiles must fail closed.
- Node offline/unreadable means partial/failed data, never zero usage.
