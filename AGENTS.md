# Qasedak agent quick start

This is a Django store with a website, Telegram/Bale bots, payment review, panel adapters, and customer subscription Cups. This file is an index, not a substitute for the code. Start with `git status --short --branch` and the task's entry point; use `rg` inside that area before opening more files.

## Pick one map

| Task | Read first |
| --- | --- |
| Locate a subsystem or model | [Project map](docs/agent/PROJECT_MAP.md) |
| Trace checkout, provisioning, delivery, feed refresh, or subscription HTTP | [Runtime paths](docs/agent/RUNTIME_PATHS.md) |
| Work on a previously reported issue | [Open investigations](docs/agent/OPEN_INVESTIGATIONS.md) |
| Install, operations, product features | `README.md` and the relevant document under `docs/` |

## Code boundaries

- The V2 plan contract is `PlanDeliveryConfig` + `PlanDeliverySource` (`store/models.py`). Resolve it in `store/plan_delivery_services.py`; execute it in `store/plan_delivery_execution.py`. Old routes and recipes have separate compatibility paths.
- Customer-facing delivery goes through `store/customer_delivery.py`. For V2 `SUBSCRIPTION`, the URL belongs to the Qasedak `SubscriptionCup`, the count is active Cup items backed by active links, and direct links stay empty. A missing V2 Cup is an unready delivery, never a provider URL fallback. Keep legacy behavior within the legacy branches.
- Preserve `ConfigLink.raw_link` when rendering Cups. Provider subscription URLs and remote credentials belong only to internal integration/feed code. Inspect and log safe metadata, not full URLs, tokens, raw configs, UUIDs, or customer/payment credentials.
- `store/bots.py` is a compatibility facade; new bot behavior lives under `store/telegram_bot/`. Read that package's `README.md` before changing bot imports.
- Avoid production DB tests or live panel/Telegram calls for ordinary development. Use mocks and a local test DB. For a requested live check, scope it to the stated disposable resources.

## Smallest useful verification

`manage.py` defaults to `core.settings.development`; CI uses Python 3.12 and `requirements.txt`. With dependencies installed, choose the relevant test class first, then widen only for affected integration boundaries:

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test store.tests.SubscriptionCupMVPTests
python manage.py test store.tests payments.tests
git diff --check
```

For docs-only edits, check paths and symbols plus `git diff --check`. For UI work, inspect the template and shared JS/CSS and verify rendered behavior; for admin CSS, see `npm run build:admin-css` in `README.md`. If an edit changes a mapped entry point, update the corresponding agent document in the same change. Keep these documents concise and treat current code and tests as authoritative.
