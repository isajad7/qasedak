# Post-Install Setup

Minimal install only prepares the Qasedak infrastructure. Complete the business setup from Django Admin before taking real orders.

After install, start from the Admin home dashboard:

```text
/admin/
```

It provides permission-aware KPI cards, daily workspaces, action items, recent activity, and collapsed advanced Django model management.

The detailed owner dashboard is also available at:

```text
/admin/store/dashboard/
```

The dashboard shows today’s orders, pending receipts, revenue, active/expiring services, saved panel health, Telegram configuration status, Revenue Engine status, and action items. It is an admin UI overview, not a replacement for doctor.

For aggregate business reporting, use the Reports Center:

```text
/admin/store/reports/
```

It provides today, 7-day, 30-day, this-month, previous-month, and custom date filters, plus CSV exports at `/admin/store/reports/export/`. Period metrics such as sales, customers, reminders, support, campaigns, and Revenue Engine logs are intentionally separate from current-state metrics such as active services, expired services, services expiring in 3 days, and current remaining traffic. Panel usage with unknown or insufficient quality means the saved DB snapshots are missing/partial; it should not be interpreted as zero consumption. CSV exports are aggregate/redacted and avoid secrets, full phone/email values, config links, UUIDs, card numbers, Telegram IDs, and proxy credentials.

For the daily owner workflow, use the order workbench:

```text
/admin/store/orders/workbench/
```

Use it after setup to follow the normal flow: pending receipt -> review order -> approve/reject payment -> delivery status. Approval, rejection, and retry actions are POST-only and require explicit confirmation because they may call X-UI/Sanaei or Telegram through the central provisioning services. On modern 3X-UI panels, paid orders are deferred until approval, then an enabled client is created and verified on the frozen panel/node/inbound scope.

For customer and VPN service operations, use the service workbench:

```text
/admin/store/services/workbench/
```

Owner flow: customer -> service -> usage/expiry -> resend/update/disable. The workbench and review GET pages read local DB state only. Resend config, refresh config link, update usage, disable, and enable are explicit POST actions with CSRF and confirmation. Full config links, UUIDs, tokens, passwords, proxies, and full phone/email values are not shown in the UI.

For support and customer communication, use the support workbench:

```text
/admin/store/support/workbench/
```

Use `/admin/store/support/<id>/review/` to review a single conversation, see masked customer/order/service context, reply with a prepared template, close/reopen the ticket, or mark it for follow-up. Replies are POST-only, require CSRF and explicit confirmation, and never run from a GET page.

For a one-off message to a single customer, use:

```text
/admin/store/customers/<id>/message/
```

This page only targets that customer. If the customer has no active Telegram target, sending is blocked with a safe error. It has no group audience selection; use the existing campaign/broadcast workflow separately and cautiously for group messaging.

For group messaging campaigns, use the Campaign Workbench:

```text
/admin/store/campaigns/
```

The owner flow is draft message, audience selection, DB-only preview, exact `SEND_CAMPAIGN_<id>` confirmation, then queue processing outside the web request. The confirm page creates safe `BroadcastRecipient` rows and marks the campaign queued; it does not send Telegram messages. Process the queue with:

```bash
/opt/qasedak/venv/bin/python /opt/qasedak/manage.py process_broadcast_queue --batch-size 50
```

See `docs/CAMPAIGNS.md` for retry, cancel, safe CSV export, blocked/no-target behavior, and the difference between direct customer messages, broadcast campaigns, and Revenue Engine.

For Revenue Engine operations, use the Revenue Control Center:

```text
/admin/store/revenue/control/
```

Dry-run means the Revenue Engine can create `RevenueOfferLog` rows and reports without sending real customer messages. Keep real-send off until dry-run metrics, failed logs, Telegram target coverage, and caps/cooldowns look safe. The reset safe defaults action returns the Store to enabled + dry-run with conservative limits.

Use the Setup Center to complete installation:

```text
/admin/store/setup/
```

The Setup Center shows local DB/status cards for Store identity, payment, Telegram, Telegram proxy, X-UI/Sanaei panel, inbounds, plans, routes, integration checks, and Revenue Engine dry-run/real-send status. It does not run live Telegram or X-UI calls.

For a guided owner-facing flow, use the setup wizard:

```text
/admin/store/setup/wizard/
```

The wizard walks through Store identity, payment, Telegram, optional Telegram proxy, panel, inbounds, plans, routes, and final review. Sensitive fields are write-only/masked; leaving a saved token, password, proxy, or card field blank keeps the previous value.

## Checklist

1. Open Django Admin, then go to `/admin/store/dashboard/`.
2. Review action items, then open `/admin/store/setup/wizard/` to complete the short guided forms.
3. Review Store identity: name, English name, domain, support links, and active status.
4. Configure payment/card settings: card number, card owner, bank details, receipt behavior, and SMSForwarder webhook token if used.
5. Create or update `BotConfiguration` for Telegram.
6. Test the Telegram bot from admin or with `doctor.sh --live-bot --no-fail` only after the token and admin IDs are configured.
7. Configure Telegram proxy settings if the server needs a proxy to reach Telegram.
8. Create a `Panel` for X-UI/Sanaei with URL, username, password, and optional panel proxy.
9. Sync or create at least one active `Inbound` for new orders.
10. Create public `Plan` records for the packages you want to sell.
11. Create `PlanInboundRoute` records from each sellable plan to an available inbound.
    - For modern 3X-UI routes, run the compatibility audit first and expect deferred paid provisioning.
    - For failed modern approvals, inspect with `./venv/bin/python manage.py reconcile_order_provisioning --order-id <id> --dry-run --verbose`.
12. Run non-live doctor/checks:

```bash
/opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --no-fail
```

13. Open `/admin/store/orders/workbench/` and review pending receipts from the owner-facing order review page.
14. Open `/admin/store/services/workbench/` and confirm active/expiring/expired services, Telegram targets, usage snapshots, and route/panel/inbound health.
15. Open `/admin/store/support/workbench/` and verify support queues, review links, reply templates, and no-target warnings.
16. Open `/admin/store/campaigns/` and verify a draft campaign can preview audience counts without sending. Do not queue a real campaign until BotConfiguration, target coverage, and processor command scheduling are ready.
17. Test a purchase end to end: storefront or bot order, payment submission, admin approval/rejection, configuration delivery, then service review/resend.
18. Open `/admin/store/revenue/control/` and keep Revenue Engine in dry-run at first. Review `RevenueOfferLog`, the 7-day metrics, failed logs, and command reports before any gradual real-send rollout.
19. Open `/admin/store/reports/` to review sales, customers, services, panel usage, operations, Revenue Engine, support, and campaign trends before launch.
20. Run `./venv/bin/python manage.py sync_staff_roles --apply`, then open `/admin/store/staff/` to create non-superuser staff accounts with role presets.

## Notes

- Missing Telegram, Panel, Inbound, Plan, or Route records after a minimal install are expected setup warnings, not installer failures.
- If a public sellable plan exists but no route or available inbound can provision it, fix that before opening sales.
- Live Telegram and X-UI checks are opt-in because they call external services.
- The installer is intentionally minimal; Telegram, X-UI/Sanaei, plans, routes, payment details, and Revenue Engine rollout are completed from Django Admin.
- The dashboard reads DB/log state only. For deployment checks, continue using `doctor.sh --no-fail` and opt in to live checks explicitly.
- The Reports Center GET page and CSV export read DB/log state only. They do not send Telegram messages, call X-UI/Sanaei, run broadcasts, run reminders, or start Revenue Engine scans.
- The wizard does not run Telegram or X-UI live checks automatically.
- The order workbench GET pages read DB state only. The approve/reject/retry buttons require POST confirmation before any external side effect can happen.
- The service workbench GET pages read DB state only. Live usage refresh, config link refresh, Telegram resend, and enable/disable are opt-in POST actions.
- Support review and direct customer message sends are POST-only, CSRF-protected, and require explicit confirmation. They do not provide group audience selection.
- Campaign preview pages read DB state only. Queue confirmation is POST-only, requires `SEND_CAMPAIGN_<id>`, and processing is done by `process_broadcast_queue`.
- The Revenue Control Center GET page reads DB/log state only. Real-send is POST-only, requires `ENABLE_REAL_REVENUE_SEND`, and is blocked when local safety checks fail.
- Staff Access Center uses Django Groups with product role presets. Store Owner is not the same as Django superuser; keep at least one active superuser for emergency recovery.
