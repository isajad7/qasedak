# Post-Install Setup

Minimal install only prepares the Qasedak infrastructure. Complete the business setup from Django Admin before taking real orders.

## Checklist

1. Open Django Admin.
2. Review Store identity: name, English name, domain, support links, and active status.
3. Configure payment/card settings: card number, card owner, bank details, receipt behavior, and SMSForwarder webhook token if used.
4. Create or update `BotConfiguration` for Telegram.
5. Test the Telegram bot from admin or with `doctor.sh --live-bot --no-fail` only after the token and admin IDs are configured.
6. Configure Telegram proxy settings if the server needs a proxy to reach Telegram.
7. Create a `Panel` for X-UI/Sanaei with URL, username, password, and optional panel proxy.
8. Sync or create at least one active `Inbound` for new orders.
9. Create public `Plan` records for the packages you want to sell.
10. Create `PlanInboundRoute` records from each sellable plan to an available inbound.
11. Run non-live doctor/checks:

```bash
/opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --no-fail
```

12. Test a purchase end to end: storefront or bot order, payment submission, admin approval, and configuration delivery.
13. Keep Revenue Engine in dry-run at first. Review `RevenueOfferLog` and reports before any gradual real-send rollout.

## Notes

- Missing Telegram, Panel, Inbound, Plan, or Route records after a minimal install are expected setup warnings, not installer failures.
- If a public sellable plan exists but no route or available inbound can provision it, fix that before opening sales.
- Live Telegram and X-UI checks are opt-in because they call external services.
