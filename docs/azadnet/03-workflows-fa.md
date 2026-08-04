# جریان‌های کاری قاصدک

آخرین بررسی: 2026-08-04

## 1. راه‌اندازی فروشگاه

مسیر اصلی ادمین:

```text
/admin/
/admin/store/setup/
/admin/store/setup/wizard/
```

روند:

1. Store identity، دامنه و اطلاعات پشتیبانی تنظیم می‌شود.
2. کارت پرداخت، مالک کارت، بانک و تنظیمات رسید ثبت می‌شود.
3. BotConfiguration برای Telegram ساخته می‌شود.
4. Panel برای X-UI/Sanaei ثبت می‌شود.
5. Inboundها دستی ساخته یا از پنل sync می‌شوند.
6. Planها ساخته می‌شوند.
7. PlanInboundRoute از هر پلن sellable به inbound ساخته می‌شود.
8. Revenue Engine در حالت dry-run نگه داشته می‌شود.
9. checklist با `setup_readiness` مشخص می‌کند فروشگاه آماده فروش است یا نه.

فروش فقط وقتی باز می‌شود که `Store.setup_status = ready` و store فعال باشد.

## 2. خرید از وب

مسیرهای اصلی:

```text
/
/order/<public_id>/
/dashboard/
/checkout/<tracking_code>/
```

روند:

1. کاربر وارد home می‌شود.
2. اگر sales mode اپراتورمحور باشد، اپراتور انتخاب می‌کند.
3. پلن عمومی یا حجم دلخواه انتخاب می‌شود.
4. کاربر رسید تصویری و در صورت نیاز کد تخفیف را وارد می‌کند.
5. `create_manual_payment_order` سفارش را می‌سازد.
6. سیستم route مناسب را با `select_inbounds_for_plan` پیدا می‌کند.
7. برای پنل legacy ممکن است client غیرفعال از همان ابتدا ساخته شود.
8. برای پنل modern، ساخت client تا تایید پرداخت deferred می‌شود.
9. سفارش به `pending_verification` می‌رود و ادمین notification می‌گیرد.
10. کاربر صفحه order detail را می‌بیند و بعدا از dashboard وضعیت و کانفیگ‌ها را دنبال می‌کند.

## 3. خرید از ربات Telegram/Bale

نقطه ورود:

```text
store/bots.py
store/telegram_bot/router.py
```

روند کاربر:

1. `/start` یا دکمه خرید سرویس
2. بررسی عضویت کانال در صورت فعال بودن force join
3. انتخاب اپراتور در حالت operator-based
4. انتخاب پلن یا حجم دلخواه
5. انتخاب تعداد
6. اعمال کد تخفیف یا رد کردن آن
7. نمایش خلاصه خرید و اطلاعات پرداخت
8. دریافت نام دلخواه کانفیگ یا رد کردن
9. دریافت عکس رسید
10. ثبت سفارش با metadata منبع ربات
11. reset state و نمایش خلاصه سفارش

ربات stateهای خرید و پرداخت را در `BotUser.state` و `BotUser.state_data` نگه می‌دارد.

## 4. جلوگیری از سفارش تکراری

در `store/order_services.py` سفارش‌های مشابه با کلید ترکیبی زیر کنترل می‌شوند:

- store
- customer
- plan
- operator
- quantity
- نام پرداخت‌کننده
- زمان پرداخت
- tracking بانکی

اگر سفارش مشابه در پنجره زمانی اخیر وجود داشته باشد، همان سفارش برگردانده می‌شود و تلاش تکراری در metadata ثبت می‌شود.

## 5. بررسی پرداخت و تایید ادمین

مسیرهای اصلی:

```text
/admin/store/orders/workbench/
/admin/store/orders/<id>/review/
```

روند:

1. ادمین سفارش‌های pending یا رسیدهای تازه را در workbench می‌بیند.
2. وارد review سفارش می‌شود.
3. اطلاعات پرداخت، رسید، تحلیل رسید، مشتری، پلن، route و وضعیت delivery را بررسی می‌کند.
4. با POST تایید یا رد می‌کند.
5. تایید به `activate_order` و سپس `approve_and_provision_order` وصل می‌شود.
6. رد کردن دلیل rejection ثبت می‌کند و سفارش rejected می‌شود.
7. retry delivery یا retry provisioning برای خطاهای قابل بازیابی قابل استفاده است.

## 6. SMSForwarder و match پرداخت

مسیر webhook:

```text
/webhooks/smsforwarder/
```

روند:

1. webhook فقط با token معتبر پذیرفته می‌شود.
2. متن پیامک از JSON، POST یا body استخراج می‌شود.
3. `parse_payment_sms` مبلغ واریز، موجودی و تاریخ/زمان شمسی را parse می‌کند.
4. `find_matching_orders` سفارش‌های pending با مبلغ برابر و پنجره زمانی نزدیک را پیدا می‌کند.
5. اگر match وجود داشته باشد، ادمین‌ها notification می‌گیرند.
6. تایید SMS، سفارش را verified می‌کند و سپس `approve_and_provision_order` را صدا می‌زند.

این مسیر اتوماتیک‌سازی کمک‌کننده است، ولی همچنان برای matchهای چندتایی نیاز به انتخاب دقیق سفارش دارد.

## 7. Provisioning و تحویل کانفیگ

مسیر مرکزی:

```text
store/provisioning_services.py::approve_and_provision_order
```

روند:

1. سفارش با lock از DB خوانده می‌شود.
2. اگر قبلا completed و verified باشد، idempotent برمی‌گردد.
3. strategy بر اساس renewal/admin_direct/paid و profile پنل انتخاب می‌شود.
4. برای renewal، client موجود renew می‌شود.
5. برای legacy، clientهای inactive موجود فعال می‌شوند یا missingها ساخته می‌شوند.
6. برای modern، client فعال با identity deterministic ساخته، دوباره از remote خوانده و verify می‌شود.
7. `VPNClient` local upsert می‌شود و active می‌شود.
8. `Order` به completed و provisioning به provisioned می‌رود.
9. Subscription Cup برای order بازسازی می‌شود.
10. Referral reward ایجاد می‌شود.
11. notification approved برای مشتری ارسال می‌شود.

## 8. تمدید سرویس

مسیرهای کاربر:

```text
/access/<config_id>/renew/
ربات: user:renew
```

روند:

1. کاربر کانفیگ قابل تمدید را انتخاب می‌کند.
2. برای آن VPNClient سفارش renewal ساخته می‌شود.
3. سفارش تا ارسال رسید pending می‌ماند.
4. بعد از تایید پرداخت، `renew_client` روی پنل اجرا می‌شود.
5. order completed می‌شود و لینک/وضعیت به کاربر اطلاع داده می‌شود.

سیستم جلوی ساخت چند تمدید pending برای یک کانفیگ را می‌گیرد.

## 9. تست رایگان

مسیر ربات:

```text
user:free_trial
```

روند:

1. تنظیمات free trial از Store خوانده می‌شود.
2. فعال بودن store، panel، inbound، حجم، مدت و cooldown بررسی می‌شود.
3. اگر کاربر در cooldown باشد، زمان مجاز بعدی اعلام می‌شود.
4. درخواست با lock ساخته می‌شود.
5. client کوتاه‌مدت روی inbound تست ساخته می‌شود.
6. `FreeTrialRequest` و `VPNClient` ثبت و کانفیگ برای کاربر ارسال می‌شود.

## 10. Referral و جایزه دعوت

روند:

1. هر Customer کد referral دارد.
2. لینک دعوت تلگرام با `start=ref_<code>` ساخته می‌شود.
3. وقتی دعوت‌شده اولین خرید موفق خود را کامل کند، `ReferralRewardLedger` برای دعوت‌کننده ساخته می‌شود.
4. دعوت‌کننده می‌تواند جایزه GB/روز را روی یکی از کانفیگ‌های فعال خود redeem کند.
5. redeem با عملیات X-UI برای افزودن حجم و تمدید مدت همراه است.

## 11. پشتیبانی

مسیرهای وب و ادمین:

```text
/support/
/support/messages/
/support/send/
/admin/store/support/workbench/
/admin/store/support/<id>/review/
```

مسیر ربات:

```text
user:support
```

روند:

1. کاربر دسته پشتیبانی و پیام را ثبت می‌کند.
2. `SupportConversation` و `SupportMessage` ساخته می‌شود.
3. ادمین‌ها notification می‌گیرند.
4. پشتیبان در workbench زمینه مشتری، سفارش و سرویس را با داده‌های mask شده می‌بیند.
5. پاسخ با POST ارسال می‌شود و conversation answered یا closed می‌شود.

## 12. کمپین و broadcast

مسیر:

```text
/admin/store/campaigns/
```

روند:

1. کمپین draft ساخته می‌شود.
2. audience و channel انتخاب می‌شود.
3. preview فقط از DB خوانده می‌شود و ارسال انجام نمی‌دهد.
4. ادمین با عبارت دقیق `SEND_CAMPAIGN_<id>` کمپین را queue می‌کند.
5. `BroadcastRecipient`ها ساخته می‌شوند.
6. دستور `process_broadcast_queue` ارسال را batch و rate-limited انجام می‌دهد.
7. review و CSV امن وضعیت گیرنده‌ها را نشان می‌دهد.

## 13. Revenue Engine

Triggerها از خرید، انتخاب پلن، checkout، payment screen، مصرف بالا، نزدیک انقضا، انقضا، inactivity و retention events می‌آیند.

روند:

1. Rule Engine تصمیم اولیه می‌سازد.
2. Optimization/AI ممکن است variant یا پیام را تغییر دهد.
3. Guardrails ارسال را بر اساس dry-run، cooldown، cap، quiet hours و target معتبر کنترل می‌کند.
4. اگر dry-run باشد فقط `RevenueOfferLog` ساخته می‌شود.
5. اگر real-send مجاز باشد پیام از طریق Telegram ارسال و log می‌شود.
6. خرید بعد از offer به conversion وصل می‌شود.

مسیر ادمین:

```text
/admin/store/revenue/control/
```

## 14. مانیتورینگ پنل، usage و reconciliation

دستورها و مسیرها:

```text
check_panel_health
collect_panel_usage_snapshots
calculate_panel_daily_usage
reconcile_vpn_clients
/admin/store/panel-center/
/admin/store/services/workbench/
```

روند:

1. Panel Center اتصال و capability پنل را بررسی می‌کند.
2. sync topology اطلاعات inbound/node را از 3X-UI می‌خواند.
3. health check وضعیت login، inboundها و خطاهای سازگاری را ثبت می‌کند.
4. usage snapshot مصرف کل پنل و clientها را ذخیره می‌کند.
5. daily usage اختلاف snapshotها را برای گزارش روزانه محاسبه می‌کند.
6. reconciliation، local VPNClient را با remote scope دقیق panel/node/inbound مقایسه می‌کند.
7. cleanup فقط local soft-delete برای remote_missing تاییدشده است و remote delete انجام نمی‌دهد.

## 15. بکاپ، ریستور و انتقال سرور

مسیر:

```text
/admin/store/backups/
scripts/backup.sh
scripts/restore.sh
```

روند:

1. backup job ساخته می‌شود.
2. بسته backup با DB و در صورت نیاز media/env/system ساخته می‌شود.
3. restore package در Admin upload و validate می‌شود.
4. Admin فقط command امن restore را تولید می‌کند.
5. اجرای destructive restore بیرون از request وب و از طریق script انجام می‌شود.

## 16. Staff Access

مسیر:

```text
/admin/store/staff/
sync_staff_roles
```

نقش‌ها:

- Store Owner
- Order Operator
- Support Agent
- Finance
- Catalog Manager
- Technical Operator
- Marketing Manager
- Analyst Read Only

این نقش‌ها capabilityهای محصولی را به permissionهای Django map می‌کنند. Store Owner با superuser یکی نیست.

## 17. Tenant و deployment چندمستاجری

مسیرهای API:

```text
/orchestrator/v2/server/register
/orchestrator/v2/instance/create
/orchestrator/v2/instance/deploy
/orchestrator/v2/instance/status
```

دستورها:

```text
create_tenant
tenant_status
restart_tenant
suspend_tenant
resume_tenant
enable_tenant_https
```

روند کلی:

1. ServerNode ثبت می‌شود.
2. TenantInstance ساخته می‌شود.
3. پورت، کانتینر، env، DB و Nginx آماده می‌شود.
4. container با image قاصدک اجرا می‌شود.
5. health check و runtime_state ثبت می‌شود.
6. owner toolkit امکان start/stop/restart/bot worker/HTTPS را مدیریت می‌کند.

