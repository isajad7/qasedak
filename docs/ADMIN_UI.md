# Admin UI

The Django Admin home page is customized as a Qasedak product dashboard while keeping the Jazzmin shell.

It shows:

- a compact Persian RTL header with current status
- KPI cards for orders, payments, revenue, services, support, and setup when permitted
- permission-aware workspace cards for daily operations, growth/management, and setup
- grouped Jazzmin sidebar sections ordered for store-owner and seller workflows: Dashboard, Sales, Customers, Infrastructure, Marketing & Revenue, Reports, and Management
- action-required items from saved DB state only
- Service Workbench reconciliation controls for comparing local VPN clients with X-UI using POST-only checks and local soft-delete of confirmed remote-missing clients
- recent activity with sensitive values redacted
- advanced Django model management collapsed at the bottom, rendered from the standard admin app list so model permissions still apply

The Product Catalog page at `/admin/store/catalog/` is a focused plan-management screen. It intentionally renders only the catalog header actions, GET-only filters, the plan table, and a compact empty state. Store status, setup guidance, command hints, inbound readiness summaries, and broad route overviews belong on the dashboard, setup center, or infrastructure/admin model pages instead.

The dashboard CSS is scoped to `tw-*` classes and compiled ahead of time:

```bash
npm run build:admin-css
```

Source:

```text
static_src/admin/qasedak_admin_tailwind.css
tailwind.admin.config.js
```

Compiled artifact:

```text
static/admin/qasedak_admin_tailwind.css
```

Tailwind preflight is disabled and no CDN is used. Production only serves the compiled static file and does not need Node.js to render the Admin UI.

Service reconciliation details, status meanings, command usage, and safety rules are documented in [Service Reconciliation](SERVICE_RECONCILIATION.md).
