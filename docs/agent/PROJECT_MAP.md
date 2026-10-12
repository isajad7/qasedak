# Qasedak project map

Read only the row for the task. Paths and symbols below were checked against `main` at `5ded3f2`; verify the current checkout before relying on them.

## Entry points and owners

| Area | First files and symbols |
| --- | --- |
| HTTP, configuration | `core/urls.py` includes `store.urls` and `payments.urls`; `core/settings/{base,development,production}.py`; `manage.py` defaults to development. |
| Domain | `store/models.py`: `Store`, `Plan`, `Customer`, `Panel`, `Inbound`, `Order`, `VPNClient`; delivery models listed below. Read the class you need, not the whole file. |
| Website purchase | `store/urls.py` -> `store/views.py` (`home`, `create_order_from_checkout`, `checkout`, `order_detail`); `store/order_services.py` (`canonical_delivery_for_checkout`, `create_manual_payment_order`). |
| Payment/review | `store/admin_views.py` (`handle_order_review_action`), `store/order_actions.py` (`activate_order`, `reject_order`), `store/provisioning_services.py` (`approve_and_provision_order`); SMS webhook and matching are in `payments/{views,payment_matching}.py`. |
| V2 plan setup | `store/models.py` (`PlanDeliveryConfig`, `PlanDeliverySource`); `store/plan_delivery_services.py` (`resolve_plan_delivery_configuration`, `active_delivery_sources`); admin editor in `store/admin_catalog.py` and `store/admin_plan_fulfillment.py`. |
| Fulfillment | `store/plan_delivery_execution.py` (`execute_plan_delivery`, `_ensure_subscription_cup`); inventory allocation in `store/config_inventory_services.py`; older recipes in `store/cup_fulfillment_services.py`; older routes in `store/plan_route_services.py`. |
| Panel providers | `store/panels/factory.py`, `store/panels/{pasarguard,xui}/adapter.py`, `store/panels/pasarguard/client.py`; older X-UI operations in `store/xui_api.py`. |
| Dynamic source | `store/external_subscription_sources.py` (`register_external_subscription_feed_snapshot`, `refresh_external_subscription_feed`, `filter_native_configs`); command `store/management/commands/refresh_external_subscription_feeds.py`. |
| Customer delivery | `store/customer_delivery.py` (`CustomerDeliveryResolver`, `resolve_customer_order_delivery`, `resolve_customer_client_delivery`, `customer_delivery_link_groups`, `customer_visible_config_count`). |
| Cup and HTTP | `store/subscription_cups.py` (active links, rendering and URL builders); `store/views.py` (`wants_dashboard`, `subscription_cup`); routes in `store/urls.py`. |
| Website UI | `templates/{order_detail,dashboard,config_detail,my_configurations}.html`; reusable CTA in `templates/includes/config_links.html`; copy interaction in `templates/base.html`; Cup dashboard in `templates/store/subscription_cup/dashboard.html`. |
| Bot | `store/bots.py` compatibility facade; `store/telegram_bot/{router,buy_flow,order_finalizers,order_delivery,services_flow,notifications}.py`; detailed map in `store/telegram_bot/README.md`. |
| Admin/operations | `store/admin_panel_center/`, `store/admin_cup_center/`, `store/admin_views.py`, `store/management/commands/`, `scripts/`, `docs/{INSTALL,CONFIGURATION,TROUBLESHOOTING,RELEASE}.md`. Other features: `store/{revenue_engine,orchestrator_v2,deployment}/`. |

## Delivery data relationships

| Entity | Role |
| --- | --- |
| `PlanDeliveryConfig` -> `PlanDeliverySource` | Delivery mode and ordered panel/inventory sources for a plan. |
| `Order` -> `VPNClient` | Purchase state and provisioned remote identity; V2 checkout freezes delivery metadata on the order. |
| `SubscriptionCup` -> `CupItem` -> `ConfigLink` | Customer token, selected ordered items, and preserved native `raw_link` values. Cup can belong to an order and/or client. |
| `ExternalSubscriptionFeed` -> `SubscriptionCup` | Internal provider subscription reference, filter policy, refresh state and reconciliation into the same Cup. |

All these models are in `store/models.py`. `SUBSCRIPTION`, `DIRECT_LINKS`, and `GLOBAL_FALLBACK` are modes on `PlanDeliveryConfig`. V2 subscriptions can combine panel and inventory sources. The resolver isolates customer output from upstream links; do not infer customer config count from source or group count.

## Focused lookup

```bash
rg -n '^class (Order|PlanDeliveryConfig|PlanDeliverySource|SubscriptionCup|CupItem|ConfigLink|ExternalSubscriptionFeed)\b' store/models.py
rg -n '^def (create_manual_payment_order|approve_and_provision_order|execute_plan_delivery|refresh_external_subscription_feed)\b' store
rg -n 'resolve_customer_order_delivery|customer_delivery_link_groups' store/views.py store/telegram_bot
rg -n 'subscription_cup|wants_dashboard' store/urls.py store/views.py store/tests.py
rg -n 'class (SubscriptionCupMVPTests|WebCheckoutReceiptTests|ModernPaidProvisioningTests|TelegramPurchaseFlowTests)\b' store/tests.py
```

Tests are mostly in `store/tests.py` (large) and `payments/tests.py`, with smaller tests under feature packages. `scripts/release_check.sh` and `.github/workflows/ci.yml` define release and CI checks. Documentation for other operational features already exists under `docs/`; search by the feature name instead of duplicating it here.
