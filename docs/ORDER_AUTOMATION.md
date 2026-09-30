# Order receipt follow-up and timed approval

Owner controls live at `/admin/store/orders/workbench/`, per selected store.

- Receipt reminders default **on**. Orders waiting for manual-card review are batched (up to 10 per message), at 5, 15 and 30 minutes after receipt submission, then hourly. Resolved orders stop generating reminders. Failed notification attempts do not permanently claim success.
- Five-minute automatic approval defaults **off**. It is a time-based trust decision, **not bank payment verification**. Enabling it also gives existing receipts five minutes from enablement before they can be approved. Orders without submitted payment evidence, gateway/admin-free payments, closed orders and disabled stores are excluded.
- `OrderAutomation.auto_approved_at` records the automatic decision before calling the normal provisioning flow. Both purchases and renewals use that flow. A timeout or failed attempt remains visible for manual review; the scheduler does not repeatedly create/renew remote services when the result is uncertain.
- The workbench has a paginated pending-reconciliation queue and full automatic-approval history. Review pages expose the decision time, reviewer, note, provisioning errors and cancellation-message status. “Payment reconciled” records the human check without reprovisioning; referral rewards are withheld until this check succeeds.
- Cancellation requires `orders.reject`, CSRF, a confirmation and a reason. It closes the order's subscription Cups and disables its dedicated panel clients, preserving order/audit history. A renewal cancellation disables the existing renewed service; it does not reconstruct old quota/expiry or undo already consumed traffic. A later completed renewal blocks cancellation of the same service pending manual investigation.
- Shared/external config credentials already downloaded cannot be revoked by disabling a project Cup. The review page explicitly explains that upstream intervention is required for those credentials; shared upstream records are never modified globally.
- If any panel disable fails, status is **cancel_failed**, previous successful steps are retained, and retry continues remaining targets. Approval is blocked while cancellation is pending/incomplete. The order is marked cancelled only after dedicated clients are disabled.
- A cancellation message is sent to active Telegram targets for the customer/store. Failed delivery is retried after five minutes; successful messages and customers with no target are not repeatedly processed. Telegram does not provide an idempotency key: a process crash after Telegram accepts a message but before its result is saved can cause a repeated message.

## Runtime

`docker/entrypoint.sh` starts `docker/start-order-automation.sh` after migrations, independently of the Revenue worker. It processes jobs every 30 seconds, so the five-minute decision is applied on the next tick. The kill switch is `QASEDAK_ORDER_AUTOMATION_ENABLED=false`; store auto-approval remains a separate opt-in setting.

```
python manage.py process_order_automation --limit 100
python manage.py process_order_automation --check-running
```

For installations outside the Docker entrypoint, schedule the first command every minute. Do not run it as part of a web request. The second command is read-only; deployment checks its recent per-store heartbeat in addition to HTTP/database health.

## Code map / verification

- `store/order_automation.py`: eligibility, timer claims, reminders, reconciliation, cancellation.
- `store/models.py::OrderAutomation`: audit/progress and message delivery state; Store holds switches and heartbeat.
- `store/admin_notifications.py`: release unsuccessful initial notification claims.
- `store/admin_views.py`: capability checks, owner queue and review actions.
- `store/test_order_automation.py`: timing, repeat prevention, real purchase/renewal integration with mocked panel transport, partial cancellation, delivery retries and access checks.
- `store/migrations/0068_order_automation.py`: additive schema only; no existing orders are automatically marked approved.

Run the new suite plus `store.tests.AdminNotificationTests` and `store.tests.AdminOrderWorkbenchTests`. Full CI includes these and checks migration drift. Never test this flow by approving or cancelling real customer orders without a specific request.
