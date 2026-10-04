# Store development map

For subscription/Cup freshness, read `../docs/SUBSCRIPTION_SYNC.md` first.
Start with `subscription_sync.py`, `external_subscription_sources.py`, and
`subscription_cups.py`; PasarGuard HTTP lives in `panels/pasarguard/client.py`.
New-plan provisioning uses `plan_delivery_execution.py`; legacy/recipe delivery
also needs the same owned dynamic-feed contract. Preserve customer Cup tokens,
per-source ownership, last-good behavior and explicit disabled states.

Do not fetch panel URLs while holding database row locks or print tokens/raw links
in diagnostic commands. Meaningful freshness tests live in `test_subscription_sync.py`.
For measured consumption/outreach, read `../customer_activity/AGENTS.md` and its
linked docs rather than inferring consumption from subscription requests.
