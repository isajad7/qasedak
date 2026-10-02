# Revenue Engine: measured customer activity (stage 1)

Owner page: `/admin/store/customers/activity/` (existing `revenue.view` capability).
Linked from the owner dashboard, Revenue Control and the Customers navigation.

## Contract

- A purchase and a customer are different units. Each purchase has its own observation series; customer counts are distinct customer IDs within the selected store.
- Activity is a **positive counter increase between two successful, scoped observations**, not `last_synced_at`, a Telegram interaction, an order, a panel status or a subscription download. The default threshold is one byte; `QASEDAK_ACTIVITY_MIN_BYTES` can raise the minimum per interval to exclude probes. This is evidence of traffic, not proof of a human operating a device.
- Active in 48 hours requires an observed traffic interval entirely inside that rolling window. Inactivity requires 48 hours of uninterrupted valid observations without qualifying traffic. A first snapshot, an outage, incomplete data, counter decrease, quota/expiry change or changed source set restarts observation. Observations more than 45 minutes apart are not compared. Exact traffic times within each polling interval are unknown.
- Calendar days use Asia/Tehran. Cross-midnight intervals are excluded from daily activity instead of guessing which day traffic occurred. Daily figures are lower bounds; the denominator is distinct customers with at least one comparable interval that day, **not full-day telemetry coverage**. There is no historical backfill from lifetime counters. A dash means no comparable data, not zero customers.
- Service entitlement and consumption remain separate. New (first verified purchase within 30 days) and loyal (2+ verified purchases) labels can coexist with active/inactive. Lost means all current services are known to have ended at least seven days ago; another active, unknown or collecting purchase prevents that classification. Disabled/exhausted end time starts when first observed unless an exact remote expiry is available.
- A valid unused purchase is “needs follow-up,” even if another purchase makes its owner active. The page expands each customer's purchases with the reason, last observation and last proven consumption interval.
- Closing a local order or Cup does not establish that downloaded credentials stopped working. A remotely usable service attached to a closed order/Cup is a discrepancy; it is neither automatically revoked nor counted as lost.

## Attribution and current limitations

`mapping.py` maps direct VPN clients and Cup items to panel + local inbound + node + hashed identity. Different links to the same physical source inside one purchase are deduplicated. Sharing across purchases/customers, including unresolved or orphan Cup claims, prevents per-purchase attribution. Successful explicit renewals (`renewal_client_pk`, matching customer/store, verified order) transfer the target service to the new purchase cycle. Other independent services stay with their original purchase.

X-UI/Sanaei and PasarGuard panels are supported. Other families are explicitly unknown. X-UI requires reliable panel/inbound/node mapping. PasarGuard uses its panel-wide user account counter: the local synthetic UUID is not the remote credential, and several native links/groups for one account must not multiply traffic. Paginated `GET /api/users` responses supply `used_traffic`, `data_limit`, `expire`, status and hashed proxy identities. A native link with a declared VPN-client owner must match both that remote username and its actual credential. Failed, malformed or repeating pagination invalidates the batch; no per-user requests or subscription downloads are used. The batch is bounded to 50 pages of 200 users. The contract was checked against the official [PasarGuard user router](https://github.com/PasarGuard/panel/blob/main/app/routers/user.py) and [response models](https://github.com/PasarGuard/panel/blob/main/app/models/user.py).

Unsupported credential formats and unresolved ownership stay unknown. The collector does not infer traffic from aggregate inbound totals. Missing/malformed counters never become zero usage. Rotating/mixed shared subscription Cups can remain unmeasurable until dedicated identities and stable mappings exist. Editing an imported link or its ownership must preserve those guarantees.

The collector cannot distinguish several people using one purchased credential or detect an upstream reset that happens between polls and surpasses the old counter before the next sample. The page shows current plan volume; the app's existing mutable Plan is not an immutable historical purchase-volume ledger. These limitations must be considered before generating personalized messages in stage 2.

## Runtime and rollout

Docker starts `docker/start-customer-activity.sh` after migrations. It runs every 900 seconds **after the previous pass completes**. X-UI uses one panel login and one uncached read per configured active inbound; PasarGuard reads paginated users once per panel. These batches serve all mapped purchases; no per-customer network calls. A database lease with token fencing prevents overlapping containers from updating baselines. No panel client writes, Telegram sends, deletion, bootstrap, marketing-worker enablement, or dry-run changes occur.

```
python manage.py collect_customer_activity
python manage.py collect_customer_activity --check-running
python manage.py collect_customer_activity --status
```

`QASEDAK_CUSTOMER_ACTIVITY_ENABLED=false` disables the Docker loop. Existing per-store `panel_usage_tracking_enabled` is respected. Deployment checks collector liveness in addition to existing web/database/order-automation health. Panel outages are data-quality outcomes, not a reason to roll back otherwise healthy code. The dashboard reports them as unknown. A collector code failure is recorded separately and fails the heartbeat check.

Set a stable `DJANGO_SECRET_KEY` for local multi-process observation too: the development fallback is ephemeral per process, and hashed panel identities depend on that key. Production already requires its environment key and is unaffected by removal of the former hardcoded development fallback.

`ActivityCollector.summary` contains counts/reason codes only. `PurchaseActivity.counters` stores scoped hashes, counters, quota, expiry and timestamps; it never stores raw config credentials. `ActivityObservation` is append-only apart from normal cascade deletion of its purchase. No automatic retention deletion is introduced. Budget roughly 96 observations per monitored purchase/day and review table growth before long-term rollout at higher scale.

Migration `customer_activity/0001_initial` is additive. Existing orders, panel statistics, marketing flags and messages are untouched. After release, activity starts appearing on the second successful poll; reliable inactivity needs 48 hours. The initial production coverage must be reviewed before enabling inactivity outreach.

## Code map and next stages

- `customer_activity/panels.py`: read-only X-UI/PasarGuard normalization, bounded pagination, strict counter validity and safe error-code diagnostics.
- `customer_activity/mapping.py`: purchase/Cup/renewal attribution and shared-identity exclusion.
- `customer_activity/services.py`: evidence window, baselines, discrepancies, lease and collector.
- `customer_activity/models.py`: summary, observations, scheduler heartbeat.
- `customer_activity/views.py`, `templates/admin/store/customers/activity.html`: customer groups and daily distinct counts; GET-only, no panel calls.
- `customer_activity/tests.py`: evidence boundaries, outages, reset, renewal, sharing, multi-purchase/dedup, midnight, permissions and scheduler.

Stage 2 should consume this evidence for per-purchase support follow-up and time/volume renewal journeys. Add purchase-bound support callbacks, one message per stage/cycle, cooldowns, suppression after renewal and competing support/sales journeys, and observable delivery/retry state. Stage 3 adds bounded win-back offers and purchase conversion tracking. Existing generic retention scans still use old engagement heuristics; this stage deliberately does not activate or rewire them.

Run `python manage.py test customer_activity.tests`; CI includes the full existing project suite, migration drift and shell syntax checks.
