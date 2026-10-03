# Purchase outreach (Revenue stage 2)

Owner page: `/admin/store/customers/outreach/`. Activity evidence and attribution
contract: [CUSTOMER_ACTIVITY.md](CUSTOMER_ACTIVITY.md).

## Rules

Only fulfilled, verified purchases with fresh, complete `reason=ok` telemetry
qualify. Baselines, gaps, partial coverage, counter resets, shared identities,
unverified orders and missing counters never justify a message. Every transport
attempt rechecks the purchase, service cycle, local Cup/client changes, receipt,
recipient, settings and suppression immediately before sending.

Priority is: 48-hour inactivity, ended service, expiry within 24h, volume at most
5%, expiry within 72h, volume at most 20%. Inactivity needs uninterrupted full
coverage and a still-valid service; it has a support button rather than a sales
button. Ended-service reminders are limited to the first 48h after a known end.
Multi-source messages explicitly say “at least one source”; volume describes the
current remote quota, not an immutable historical Plan snapshot. First snapshots
of already-ended services older than that window are not a win-back campaign.

Each order/cycle/kind has one durable event. A detected counter reset, quota,
expiry or source-map change advances the cycle; network gaps alone do not.
Explicit successful renewals suppress the old purchase and record the renewal
order; a recent (7d) submitted renewal receipt pauses outreach. A separate new
purchase does not suppress a different unused purchase.

Suppression: muted customer, open/waiting-admin support ticket, outside 09–21
Asia/Tehran by default, one message per customer per rolling 24h, three per 7d,
and 50 per store per rolling 24h by default. Store hours and limit are editable.
Reservations and ambiguous sends count toward caps. Older RevenueOfferLog sends
count too; successful journeys create a legacy guard log to reduce overlapping
campaigns. This is not a shared atomic reservation with old campaign workers;
do not enable those workers as part of this rollout.

Delivery requires an active Telegram BotUser belonging to the same store's active
bot, with positive private chat ID equal to provider user ID. No admin/group chat
or unscoped/global bot fallback is allowed. Callback ownership is checked again
against the recipient, bot and order customer/store. Support messages retain
`purchase_id` and `journey_event_id`; renewal buttons use only this purchase's
clients. Mute/resume and support remain accessible without a channel-membership
gate; renewal follows the existing membership gate. Journey messages are retained
when a button is pressed so other actions remain accessible.

## Runtime and operations

`docker/start-customer-activity.sh` runs `run_customer_outreach` after each
successful collection. New settings default to **preview**, which records text
and reasons without contacting Telegram. `live` sends; `off` blocks sending.
These modes are independent of old Revenue/dry-run/worker flags.

```
python manage.py run_customer_outreach --status
python manage.py run_customer_outreach --check-running
python manage.py run_customer_outreach --store STORE_PK --set-mode preview
python manage.py run_customer_outreach --store STORE_PK --set-mode live
python manage.py run_customer_outreach --store STORE_PK --set-mode off
```

Mode-setting commands do not send immediately. Normal execution revalidates and
dispatches only eligible events. `QASEDAK_CUSTOMER_OUTREACH_ENABLED=false` stops
execution; disabling the parent activity loop also stops these journeys.
Deployment checks the heartbeat and reports aggregate status/kind/block counts;
no tokens, chat IDs or message bodies are printed. The dashboard GET is DB-only
and never prepares or sends events. POST needs CSRF and capabilities:
`revenue.view`, `revenue.manage_safe`, and `revenue.enable_real_send` for live or
retry. Settings record the modifying user and first activation time.

For the reviewed initial deployment only, `--store STORE_PK --activate-initial`
can advance an untouched preview to live. It requires recent completed collector
and outreach runs plus a fully observed verified purchase with a same-store
private Telegram recipient. It does not send immediately. A previous activation,
an owner-panel edit or a non-preview mode is preserved on later deployments.
The reviewed single-store deployment uses `--activate-initial --only-active-store`
after the health checks, then runs one normal pass and prints aggregate status.
This refuses zero or multiple active stores rather than picking a tenant. Other
deployments should use an explicit reviewed store ID. Neither mode enables old
engines. At night, activation succeeds but the regular quiet-hours rule blocks
delivery until the next permitted collection pass.

Transport state is reserved/committed before HTTP. Acknowledged messages become
`sent`. Explicit Telegram rejection becomes `failed`, with an owner-only retry
action that still rechecks current eligibility. Timeout, missing acknowledgement
or interrupted reservation becomes `uncertain` and is **never automatically
retried**. This deliberately prefers a missed message to an accidental duplicate.
It is not a guarantee of exactly-once delivery over HTTP. Do not manually reset
uncertain events without checking Telegram and the customer history.

Migration 0002 is additive: four outreach models and activity cycle. It does not
change orders, payments, panel clients or historical observations. App rollback
can leave the additional tables in place; preserve them for audit.

## Code map / tests

- `outreach.py`: journey eligibility, text, caps, reservation, transport, loop.
- `bot_flow.py`: recipient-bound support/renew/mute callbacks.
- `outreach_views.py`, `templates/admin/store/customers/outreach.html`: controls/history.
- `store/telegram_bot/router.py`, `support_flow.py`: existing bot flow integration.
- `management/commands/run_customer_outreach.py`: operations/heartbeat.
- `test_outreach.py`: evidence boundaries, caps, renewals, transport ambiguity,
  ownership, support context, callbacks, permissions and preview.

Run `python manage.py test customer_activity.tests customer_activity.test_outreach`.
CI also gates the full existing store/payment suite, drift and shell syntax.
Discounted win-back campaigns, campaign experiment reporting and automatic offer
pricing are not introduced by this stage.
