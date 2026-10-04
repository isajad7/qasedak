# Customer subscription freshness / PasarGuard

Customer `/sub/<token>` output is a stable Cup URL, not an immutable copy of the
initial panel links. Base64, raw and JSON reads refresh the Cup's active PasarGuard
feeds before serialization; browser dashboards remain DB-only. Duplicate reads
within 60 seconds reuse the latest result. Background hourly refresh remains
available even when a client does not request an update.

The request path uses short connect/read timeouts and an 8-second scheduling
budget across sources; requests already in progress may finish after that budget.
HTTP failures, empty/filter-empty results and suspicious large drops retain the
last-good list. Upstream requests bypass ordinary caches; customer responses keep
`Cache-Control: no-store`. Explicitly disabled feeds and inaccessible Cups are
never enabled by a customer read.

Legacy Cups are adopted only when panel-generated ConfigLinks have a provable
same-store/customer VPNClient owner and its saved upstream URL. A source is
identified by Cup + panel + VPNClient. Shared ConfigLink rows, ambiguous owners,
missing upstream URLs and the Cup's own URL are excluded. Manual links and other
sources are preserved. Existing tokens, purchases, payments and panel accounts
are not changed. A local rebuild preserves feed-managed items instead of restoring
the original `VPNClient.direct_link` snapshot.

`ExternalSubscriptionFeed.refresh_token` and `refresh_lease_until` provide a
database-backed 90-second lease shared by HTTP, background and manual refreshes.
Stale results cannot overwrite a newer runner or changed upstream/policy/owner;
only the matching runner releases its lease. Null `next_refresh_at` is due rather
than permanently ignored. No HTTP occurs while transaction row locks are held.

## Operations

```
python manage.py sync_pasarguard_subscriptions
python manage.py sync_pasarguard_subscriptions --refresh
python manage.py sync_pasarguard_subscriptions --refresh --public-base-url https://botsell.panelwpvideo.ir
python manage.py refresh_external_subscription_feeds
```

The repair is idempotent. `--refresh` fetches current native panel links without
bypassing drop protection, verifies each successful Cup's client serialization,
and reports safe counts/error codes only. Optional public verification checks up
to five real HTTPS Cup URLs as v2rayNG; mismatch or unreachable public output fails
the command without printing tokens or config links. The reviewed server deploy
runs repair/refresh/public verification after app health. A backup already precedes
deployment. Additive migration 0069 supplies the lease fields.
Bulk reads use 2-second connect / 4-second read timeouts and print safe progress
counts. Network diagnostics include exception class names and numeric errno only.
Deploy SSH sends keepalives, and retrying an already healthy revision repeats the
idempotent repair/verification so an interrupted repair is not silently skipped.

## Code map

- `store/subscription_sync.py`: legacy ownership binding, client-read orchestration, repair/verification.
- `store/external_subscription_sources.py`: native filters, leases/fencing, per-source reconciliation, background due selection.
- `store/subscription_cups.py`: rendering and rebuild preservation.
- `store/views.py`: client request integration, access/format/cache behavior.
- `store/panels/pasarguard/client.py`: native links HTTP and raw fallback.
- `store/test_subscription_sync.py`: end-to-end update, legacy/mixed Cups, outages, concurrency, rebuild and public verification.

Run `python manage.py test store.test_subscription_sync store.test_external_subscription_interval store.tests.SubscriptionCupMVPTests`.
CI runs the full project regression suite too. Unresolved ownership and missing
original upstream URLs require reviewed data repair; do not attach arbitrary
customers to a shared subscription merely to remove a diagnostic count.

For connection outages, `scripts/diagnostics/pasarguard_remote.sh` performs only
reads against the configured deployment and panel. Its dedicated GitHub workflow
runs on changes to that diagnostic script on `ci`, without deploying a revision.
It compares environment, direct and explicitly configured panel proxy connections,
checks DNS/TCP/TLS, and prints IDs, counts, exception classes and numeric errno.
Never print panel URLs, credentials, upstream tokens or response bodies.
