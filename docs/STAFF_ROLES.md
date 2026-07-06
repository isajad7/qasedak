# Staff Roles and Access Center

Qasedak staff access is managed with Django `User`, `Group`, and built-in model permissions. The product-facing source of truth is `store/admin_access.py`; do not hand-edit raw permission sets for normal staff.

## URLs

- Staff Access Center: `/admin/store/staff/`
- Add staff: `/admin/store/staff/new/`
- Staff review: `/admin/store/staff/<user_id>/`
- Staff edit: `/admin/store/staff/<user_id>/edit/`
- Password reset: `/admin/store/staff/<user_id>/password/`
- Role presets: `/admin/store/staff/roles/`

## Role Presets

- Store Owner: all product workflows, Staff Access Center, setup, orders, services, support, catalog, reports, campaigns, and Revenue controls. This is not the same as Django `superuser`.
- Order Operator: order/payment review, customer/service view, approve/reject where allowed.
- Support Agent: support workbench, customer/service review, replies, and config resend where allowed.
- Finance: orders, payments, reports, and report CSV export. No campaign, panel secret, staff, or Revenue real-send access.
- Catalog Manager: catalog, plans, routes, and inbound readiness. No payment/support/campaign mutation.
- Technical Operator: panels, inbounds, services, usage, and health actions. No finance, campaign, or staff access.
- Marketing Manager: campaigns and reports. No raw customer target export, payment action, service delete, or Revenue real-send.
- Analyst / Read Only: dashboard and reports summaries only. No mutation or sensitive CSV export.

## Sync Command

Dry-run:

```bash
./venv/bin/python manage.py sync_staff_roles --dry-run --verbose
```

Apply:

```bash
./venv/bin/python manage.py sync_staff_roles --apply
```

The command is idempotent. It creates missing `Qasedak ...` groups and adds missing built-in permissions. It does not remove extra permissions unless a future explicit cleanup command is added.

## Owner vs Superuser

`Store Owner` is a product role. It can manage non-superuser staff through Staff Access Center, but it should not manage Django superusers or arbitrary raw permissions.

`superuser` is the emergency/system role. Keep at least one active superuser outside daily operations and protect it with backups and strong credentials.

## Least Privilege

Use the narrowest role that matches the job. Avoid direct user permissions for ordinary staff. If a staff account needs extra access, prefer adding a new role preset in code, reviewing it, then running `sync_staff_roles --apply`.

Sensitive data is masked in product admin workflows: passwords, tokens, proxy credentials, panel secrets, full card numbers, config links, full UUIDs, and raw campaign targets should not appear in normal staff UIs.

## Emergency Recovery

If all product staff roles are broken but a superuser remains:

1. Sign in as the active Django superuser.
2. Run `./venv/bin/python manage.py sync_staff_roles --apply`.
3. Open `/admin/store/staff/`.
4. Assign a trusted staff account the Store Owner role.
5. Review audit logs and unexpected direct permissions.

If the last superuser is lost, recover from a database backup or use a controlled server shell procedure to create a new superuser. Back up the database before any access recovery work.
