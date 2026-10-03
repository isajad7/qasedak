# Open investigations

Only track actionable, unverified observations here. Remove or update an item after a tested fix; do not turn a user report into a proven root cause.

## v2rayNG import failure on some newer versions

Reported behavior: a Qasedak Cup URL imports in v2rayNG 1.10.8 for one user, while some newer versions display an update/import failure. This repository review did **not** reproduce the device failure or establish the cause.

Code facts at the reviewed commit: `store/views.py::_is_client_user_agent` recognizes `v2rayNG`; `wants_dashboard` keeps recognized client UAs on the machine path; `_normalized_subscription_format` defaults to `raw`; `?format=base64` is supported. `store/subscription_cups.py` renders active `ConfigLink.raw_link` values. Existing tests in `store.tests.SubscriptionCupMVPTests` cover raw default, explicit Base64, client detection, and endpoint access; they do not prove compatibility with all v2rayNG releases.

Next investigation: on **one disposable** Cup, compare status, redirects, content type, byte count, HTML detection, Base64 decoding and aggregate protocol counts for representative client UAs and explicit `?format=raw`/`?format=base64`. Compare the exact decoded links to stored links without printing them. Then distinguish HTTP/TLS, content negotiation, envelope, and individual config parser failures. Inspect the current upstream v2rayNG parser and verify on actual devices before changing the default format or claiming compatibility. Keep provider native links intact; do not rebuild credentials or print tokens/configs.

Quick entry points: `store/views.py::{subscription_cup,wants_dashboard}`, `store/subscription_cups.py::{render_subscription_cup_raw,render_subscription_cup_base64}`, `store/tests.py::SubscriptionCupMVPTests`.
