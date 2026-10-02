# Customer activity / Revenue stage 1

Read `../docs/CUSTOMER_ACTIVITY.md` before changing attribution, metrics or outreach.
This app measures consumption only. The existing sending engines live in
`store/revenue_engine/`; their flags are independent.

Start with `mapping.py` for ownership/Cups/renewals, `panels.py` for panel reads,
`services.py` for the 48-hour evidence window, and `views.py` for segments/charts.
Do not substitute sync timestamps, Telegram engagement or cached lifetime totals
for measured usage. Preserve unknown states on incomplete or shared telemetry.
Run `python manage.py test customer_activity.tests` after relevant changes.
