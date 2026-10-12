# Runtime paths for sales and subscriptions

Use this as a call-path index. Verify a symbol in current code before editing. Keep website, bot, and client output consistent through the existing resolver.

## Paid website order to delivery

1. `store/views.py::create_order_from_checkout` calls `store/order_services.py::create_manual_payment_order`. The order service resolves the plan's V2 delivery configuration via `canonical_delivery_for_checkout` and records `plan_delivery_v2` metadata; its legacy inbound path is separate.
2. Admin approval uses `store/admin_views.py::handle_order_review_action` -> `store/order_actions.py::activate_order` -> `store/provisioning_services.py::approve_and_provision_order`. Telegram admin approval also reaches the shared order actions. Approving may call external providers; a page GET does not perform approval.
3. When V2 delivery is active, `approve_and_provision_order` calls `store/plan_delivery_execution.py::execute_plan_delivery`: ordered sources yield links/clients; `SUBSCRIPTION` builds one order Cup, `DIRECT_LINKS` returns direct outputs. The plan's failure policy affects completion. Legacy recipe/routes have other branches.
4. For dynamic PasarGuard sources, `store/panels/pasarguard/adapter.py` gets native links; `plan_delivery_execution.py` registers an `ExternalSubscriptionFeed` for the Cup. `store/external_subscription_sources.py` filters and reconciles future refreshes into Cup items. The customer Cup token is an identity of the Cup, not the provider feed.
5. `store/customer_delivery.py::CustomerDeliveryResolver` selects what a customer sees. V2 `SUBSCRIPTION` uses the Qasedak Cup URL, zero direct links, and the count of active Cup items with active links; a missing Cup is unready. V2 `DIRECT_LINKS` uses the direct outputs. Legacy fallbacks are explicitly contained in resolver legacy paths. `CustomerDelivery.to_safe_dict()` defines a safe payload shape.
6. Website views (`dashboard`, `order_detail`, `build_config_context`, `config_detail`) pass resolved delivery to templates. `templates/includes/config_links.html` is shared CTA markup; `templates/base.html` handles `[data-copy-access-link]`. Bot order and service delivery (`store/telegram_bot/{order_delivery,services_flow}.py`) call the same resolver helpers.

## `/sub/<token>` and refresh

- Route: `store/urls.py`; HTTP logic: `store/views.py::{wants_dashboard,subscription_cup}`; link selection/renderers: `store/subscription_cups.py::{active_cup_links,render_subscription_cup_raw,render_subscription_cup_base64}`.
- Accessible Cups return machine `text/plain` to recognized client UAs, with newline-delimited native links **by default**. `?format=raw`, `?format=base64`, and `?format=json` are explicit choices. Browser dashboard selection depends on `view=dashboard` or browser UA plus HTML `Accept`; `/sub/<token>/dashboard/` forces the dashboard. Inaccessible Cups return 403 for the machine path. Check actual branches for edge cases.
- A Base64 response encodes the ordered native lines; `raw_link` remains the stored source of truth. `ExternalSubscriptionFeed.protected_subscription_url` is internal and must never replace a customer Cup URL.
- Refresh command: `python manage.py refresh_external_subscription_feeds --feed-id <disposable-id> --dry-run`. Running without `--dry-run` changes persisted Cup items and can fetch from a real provider. The command supports `--force` and `--limit`; inspect the target before using either.

## Which tests to open first

| Change | Focused tests |
| --- | --- |
| V2 checkout, Cup, feed, resolver, subscription HTTP, provider adapter | `store.tests.SubscriptionCupMVPTests` |
| Web receipt/checkout behavior | `store.tests.WebCheckoutReceiptTests` |
| Admin and remote provisioning | `store.tests.ModernPaidProvisioningTests`; relevant admin class in `store/tests.py` |
| Telegram purchase/delivery | `store.tests.TelegramPurchaseFlowTests` and Cup delivery tests |
| SMS matching/webhook | `payments.tests` |

Run `python manage.py check`, `python manage.py makemigrations --check --dry-run`, a focused test class, and `git diff --check`. Add the wider `python manage.py test store.tests payments.tests` when the changed boundary calls for it. The test runner uses its own database; never target a production database for tests.
