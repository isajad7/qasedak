# Telegram Bot Package

This package contains the Telegram/Bale bot implementation details. The legacy
`store/bots.py` module is the compatibility facade and webhook/polling
entrypoint.

## Module Map

- `client.py`: provider API client and delivery errors.
- `router.py`: update parsing, user identity helpers, and callback/message dispatch.
- `constants.py`, `formatting.py`, `keyboards.py`, `redaction.py`: shared primitives.
- `config_delivery.py`, `order_delivery.py`, `notifications.py`: shared delivery helpers.
- `user_menu.py`, `services_flow.py`, `profile_flow.py`, `user_orders_flow.py`: user account and service flows.
- `buy_flow.py`, `payment_flow.py`, `renewal_flow.py`, `order_finalizers.py`: purchase, payment, and renewal flows.
- `config_lookup_flow.py`, `free_trial_flow.py`, `referral_flow.py`, `support_flow.py`: secondary user flows.
- `admin_orders.py`, `admin_reports.py`, `admin_broadcast.py`, `admin_config_management.py`, `admin_support.py`: admin flows.

## Rules

- Do not import `store.bots` from inside this package.
- Keep dependency direction as `store/bots.py -> store/telegram_bot/*`.
- Keep callback data and bot state names backward compatible unless a migration
  or explicit compatibility path is included.
- Keep public compatibility names available from `store.bots` when existing
  tests, commands, or external callers import or patch them.
- Add new feature code to the most specific flow module; add a new module only
  when no existing flow owns the behavior.
