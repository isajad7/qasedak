# Customer activity and purchase outreach / Revenue stages 1–2

Read `../docs/CUSTOMER_ACTIVITY.md` before changing attribution, metrics or outreach.
The collector measures consumption only. `outreach.py` sends purchase journeys;
the older engines in `store/revenue_engine/` keep independent flags.

Start with `mapping.py` for ownership/Cups/renewals, `panels.py` for panel reads,
`services.py` for the 48-hour evidence window, and `views.py` for segments/charts.
Do not substitute sync timestamps, Telegram engagement or cached lifetime totals
for measured usage. Preserve unknown states on incomplete or shared telemetry.
Read `../docs/CUSTOMER_OUTREACH.md` for sending rules, operations and code map.
Use `outreach.py` for eligibility/reservations, `bot_flow.py` for recipient-bound
callbacks and `outreach_views.py` for admin controls. Never retry an ambiguous
transport result or enable older marketing workers to run these journeys.
Run `python manage.py test customer_activity.tests customer_activity.test_outreach`
after relevant changes; CI also runs the existing store/payment regression suite.
