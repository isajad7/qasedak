# Campaigns & Broadcasts

Campaigns are managed from Django Admin:

```text
/admin/store/campaigns/
```

The workflow is owner-facing and separate from direct customer messages and Revenue Engine offers.

## Owner Flow

1. Create a campaign draft at `/admin/store/campaigns/new/`.
2. Save the internal title and message text.
3. Choose an existing audience and channel.
4. Review the DB-only preview.
5. Queue with the exact phrase `SEND_CAMPAIGN_<campaign_id>`.
6. Process queued recipients outside the web request.
7. Review delivery metrics, failures, retryable failures, and safe CSV export.

GET pages read database state only. They do not create recipients, validate targets live, call Telegram, or send messages.

## Audiences

Audience choices come from `BroadcastMessage.AudienceType` and the existing broadcast/customer analytics services:

- all
- active_customers
- customers_with_active_config
- customers_without_order
- legacy_wizwiz_imported
- loyal
- good
- top_buyer
- top_referrer
- inactive
- no_order

The preview shows customers matched, BotUsers matched, targetable recipients, missing Telegram/Bale targets, inactive BotUsers, duplicates removed, estimated recipients, and limited internal samples. Full chat IDs, phone numbers, emails, UUIDs, config links, tokens, and raw metadata are not shown.

## Queue Processing

The confirm page only materializes recipients and sets the campaign status to queued. It does not send in the HTTP request.

Run the processor manually or from a scheduler:

```bash
./venv/bin/python manage.py process_broadcast_queue --batch-size 50
```

Useful options:

```bash
./venv/bin/python manage.py process_broadcast_queue --campaign-id 123 --batch-size 50 --verbose
./venv/bin/python manage.py process_broadcast_queue --campaign-id 123 --dry-run
./venv/bin/python manage.py process_broadcast_queue --campaign-id 123 --resume-failed
```

The processor is bounded and idempotent:

- It only sends pending recipients.
- It does not resend sent recipients.
- It respects the store broadcast rate limit.
- It stops safely on rate-limit or repeated transient failures.
- It continues when one recipient fails.
- It recomputes campaign counts after each batch.

## Cancel, Retry, Pause

Cancel is available for draft and queued campaigns. Sent recipients are not rolled back.

Retry is available for retryable failed recipients. Timeout/network and rate-limit failures can be moved back to pending. Blocked, forbidden, chat-not-found, invalid target, and no-target recipients are not retried.

Pause/resume is shown as unavailable because the current model has no paused status. No migration was added for this phase.

## Safe CSV

Campaign Review exports:

- internal recipient id
- customer internal pk
- BotUser internal pk
- recipient status
- safe error category
- created_at
- sent_at

It intentionally omits chat IDs, phone numbers, emails, tokens, config links, UUIDs, raw metadata, and full message text.

## Separation

Direct customer message:
Targets one selected customer from `/admin/store/customers/<id>/message/`.

Broadcast campaign:
Targets an existing audience, requires preview and `SEND_CAMPAIGN_<id>`, then uses `process_broadcast_queue`.

Revenue Engine:
Uses separate revenue logs, dry-run/real-send controls, and offer guardrails. Campaign actions do not trigger Revenue Engine sends.
