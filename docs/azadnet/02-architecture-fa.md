# معماری فنی قاصدک

آخرین بررسی: 2026-08-04

## نمای کلان

قاصدک یک پروژه Django با دو app اصلی `store` و `payments` است. `store` تقریبا همه دامنه‌های اصلی محصول را نگه می‌دارد و `payments` برای دریافت و match کردن SMS پرداخت استفاده می‌شود.

```text
core
  settings, urls, health, wsgi/asgi

store
  models, public web, admin workbenches, telegram bot,
  order/provisioning services, panel adapters, revenue engine,
  reports, backups, deployment/orchestrator, management commands

payments
  SMSForwarder webhook, SMS parser, payment matching

templates/static/scripts/docs
  UI, admin UI, install/ops docs and scripts
```

## لایه‌های اصلی

| لایه | فایل‌ها و ماژول‌های مهم | نقش |
| --- | --- | --- |
| تنظیمات و routing | `core/settings/*`, `core/urls.py`, `store/urls.py` | پیکربندی، مسیرهای public/admin/bot/orchestrator |
| مدل دامنه | `store/models.py`, `payments/models.py`, `store/orchestrator_v2/models.py` | Store، Order، Customer، VPNClient، Panel، Inbound، Campaign، Revenue logs و tenant |
| وب عمومی | `store/views.py`, `templates/*.html` | صفحه خرید، داشبورد مشتری، جزئیات سفارش، تمدید، پشتیبانی، referral |
| ربات | `store/bots.py`, `store/telegram_bot/*` | facade سازگار با legacy و جریان‌های ماژولار ربات |
| عملیات سفارش | `store/order_services.py`, `store/order_actions.py`, `store/provisioning_services.py` | ساخت سفارش، انتخاب inbound، تایید پرداخت، ساخت/فعال‌سازی کانفیگ |
| اتصال پنل | `store/xui_api.py`, `store/panels/*`, `store/xui_compat/*` | X-UI/Sanaei API، capability detection، adapter abstraction |
| Admin UI | `store/admin.py`, `store/admin_views.py`, `store/admin_dashboard.py`, `store/admin_panel_center/*` | مرکز عملیات محصولی ادمین |
| پرداخت | `payments/views.py`, `payments/sms_parser.py`, `payments/payment_matching.py`, `store/receipt_analysis.py` | رسید دستی، تحلیل رسید، SMS webhook و match پرداخت |
| رشد و مارکتینگ | `store/referral_services.py`, `store/free_trial_services.py`, `store/broadcast_services.py`, `store/revenue_engine/*` | referral، trial، campaigns، offers، retention/upsell |
| عملیات | `store/backup_restore_services.py`, `store/deployment/*`, `store/orchestrator_v2/*`, `scripts/*` | نصب، بکاپ/ریستور، tenant deployment، health/doctor |

## مدل داده اصلی

### هسته فروش

- `Store`: مرکز تنظیمات محصول، پرداخت، ربات، trial، referral، reminder، monitoring و Revenue Engine
- `Operator`: اپراتور اینترنت برای sales mode اپراتورمحور
- `Plan`: بسته قابل فروش یا داخلی، شامل حجم، مدت، قیمت، device limit و multi-inbound bundle
- `PlanInboundRoute`: route صریح از Plan و Operator به Inbound
- `Order`: سفارش مالی/عملیاتی با status، verification status و provisioning status جداگانه
- `VPNClient`: سرویس یا کانفیگ ساخته‌شده روی پنل، با وضعیت local و remote reconciliation
- `ConfigLink`, `SubscriptionCup`, `CupItem`: نگهداری و ارائه امن لینک‌های کانفیگ/اشتراک

### کاربران و ارتباطات

- `Customer`: مشتری وب/ربات، کد referral و اطلاعات تماس
- `BotConfiguration`: تنظیمات Telegram/Bale، token، admin IDs، username و join requirement
- `BotUser`: هویت کاربر در ربات و state machine خرید/پرداخت/ادمین
- `SupportConversation`, `SupportMessage`: پشتیبانی وب و ربات
- `WebTelegramLinkToken`: اتصال حساب وب به حساب تلگرام با start parameter

### پرداخت و رشد

- `IncomingPaymentSMS`: پیامک بانکی دریافت‌شده و orderهای match شده
- `DiscountCode`, `CustomerReward`, `Referral`, `ReferralRewardLedger`: تخفیف و جایزه دعوت
- `FreeTrialRequest`: درخواست و نتیجه تست رایگان
- `BroadcastMessage`, `BroadcastRecipient`: کمپین و صف گیرنده‌ها
- `RevenueOfferLog`: لاگ تصمیم، ارسال، dry-run، suppression و conversion

### عملیات پنل و سلامت

- `Panel`: پنل X-UI/Sanaei یا خانواده‌های دیگر
- `Inbound`: inbound محلی یا sync شده از node
- `PanelHealthStatus`, `PanelHealthCheckLog`: مانیتورینگ سلامت
- `PanelUsageSnapshot`, `PanelClientUsageSnapshot`, `PanelDailyUsage`, `VPNClientUsageSnapshot`: snapshot و گزارش مصرف
- `VPNClientActionLog`, `VPNClientReminderLog`: audit عملیات و یادآورها

### محصول‌سازی و tenant

- `QasedakBackupJob`, `QasedakRestoreJob`: بکاپ و ریستور
- `ServerNode`, `TenantInstance`: orchestrator چندمستاجری
- سرویس‌های deployment برای Docker، subdomain، PostgreSQL tenant DB و Nginx

## رابطه‌های مهم

```text
Store
  -> BotConfiguration
  -> Panel -> Inbound
  -> Plan -> PlanInboundRoute -> Inbound
  -> Order -> VPNClient -> ConfigLink / SubscriptionCup
  -> BroadcastMessage -> BroadcastRecipient
  -> RevenueOfferLog

Customer
  -> BotUser
  -> Order
  -> SupportConversation
  -> Referral / ReferralRewardLedger
```

## تصمیم مهم: سه وضعیت جدا برای سفارش

`Order` سه مفهوم را جدا نگه می‌دارد:

- `status`: وضعیت lifecycle سفارش مثل pending, confirmed, completed, rejected
- `verification_status`: وضعیت تایید پرداخت
- `provisioning_status`: وضعیت ساخت/فعال‌سازی سرویس روی پنل

این جداسازی باعث می‌شود تایید مالی، عملیات فنی و نمایش به مشتری با هم قاطی نشوند. مسیر مرکزی تایید و فعال‌سازی `approve_and_provision_order` در `store/provisioning_services.py` است.

## استراتژی‌های provisioning

در `store/provisioning_services.py` چهار استراتژی اصلی دیده می‌شود:

| Strategy | کاربرد |
| --- | --- |
| `legacy_precreate_inactive` | برای پنل legacy، کلاینت غیرفعال هنگام ثبت سفارش ساخته و بعد از تایید فعال می‌شود |
| `deferred_create_enabled` | برای پنل مدرن 3X-UI، سفارش paid تا تایید پرداخت remote client نمی‌سازد؛ بعد از تایید client فعال ساخته و verify می‌شود |
| `direct_create_enabled` | برای تست رایگان یا grant مستقیم، client فعال مستقیم ساخته می‌شود |
| `renew_existing_client` | برای تمدید، client موجود renew می‌شود |

برای 3X-UI مدرن، پروژه scope سفارش را freeze می‌کند: panel، node، inbound و remote key در metadata ذخیره می‌شود تا بعدا route اشتباه باعث ساخت سرویس روی مقصد نادرست نشود.

## اتصال پنل‌ها

لایه `store/panels` یک قرارداد adapter دارد. وضعیت فعلی:

- X-UI/Sanaei: مسیر عملیاتی اصلی و پشتیبانی‌شده
- 3X-UI modern single-node/multi-node: با capability profile، audit و guarded provisioning
- Marzban: adapter وجود دارد اما `not implemented yet` و safe-unsupported است
- Unknown/unsupported families: عملیات remote غیرفعال و گزارش امن تولید می‌شود

پس عبارت «اتصال به پنل‌های متفاوت» از نظر معماری درست است، اما از نظر عملیاتی فعلا X-UI/Sanaei پیاده‌سازی اصلی است و Marzban نیاز به فاز توسعه جدا دارد.

## ربات

`store/bots.py` facade سازگار با کد legacy و entrypoint webhook/polling است. پیاده‌سازی واقعی به package زیر شکسته شده:

- `router.py`: parse update و dispatch
- `buy_flow.py`, `payment_flow.py`, `order_finalizers.py`: خرید و پرداخت
- `services_flow.py`, `renewal_flow.py`, `config_lookup_flow.py`: سرویس‌ها، تمدید و مصرف
- `free_trial_flow.py`, `referral_flow.py`, `support_flow.py`: trial، referral و support
- `admin_orders.py`, `admin_reports.py`, `admin_broadcast.py`, `admin_config_management.py`, `admin_support.py`: عملیات ادمین داخل ربات

این ساختار یعنی ربات از نظر محصول یک channel کامل فروش و عملیات است، نه فقط notification layer.

## پرداخت

پرداخت دستی card-to-card در `Order` و `Store` مدل شده است. مسیرهای مکمل:

- ثبت تصویر رسید در وب و ربات
- تحلیل متن رسید با `store/receipt_analysis.py`
- دریافت webhook از `/webhooks/smsforwarder/`
- parse تاریخ شمسی و مبلغ ریالی در `payments/sms_parser.py`
- match سفارش با مبلغ و زمان در `payments/payment_matching.py`
- تایید SMS در نهایت از همان مسیر provisioning مرکزی استفاده می‌کند

در مدل `Order` فیلدهای gateway وجود دارد، اما در کد بررسی‌شده پیاده‌سازی کامل payment gateway دیده نشد. پس gateway در وضعیت آماده‌سازی/آینده است، نه فیچر کامل فعلی.

## Admin UI و امنیت عملیاتی

Admin UI بر اساس Django Admin و Jazzmin ساخته شده، اما مسیرهای سفارشی زیادی دارد. الگوی امنیتی تکرارشونده:

- GET صفحات حساس فقط DB/log state می‌خوانند
- actionهای بیرونی مثل Telegram/X-UI/ارسال پیام با POST انجام می‌شوند
- actionهای مهم confirmation دارند
- خروجی‌ها و گزارش‌ها secret، config link، UUID کامل، phone/email کامل، token و proxy credential را mask یا حذف می‌کنند
- Staff Access Center نقش‌های محصولی را با capability map اعمال می‌کند

## Revenue Engine

Revenue Engine از چند جزء ساخته شده:

- Rule Engine برای renewal/usage/retention/upsell
- Guardrails برای dry-run، cap روزانه/هفتگی، quiet hours، cooldown و target validation
- `RevenueOfferLog` برای audit و conversion
- A/B selection با `OfferSelector`
- AI optimizer با confidence و expected revenue fallback

پیش‌فرض عملیاتی امن این است که engine فعال اما dry-run باشد.

## استقرار و محصول‌سازی

پروژه از SQLite و PostgreSQL پشتیبانی می‌کند. نصب production جدید طبق docs به سمت PostgreSQL پیش‌فرض رفته و SQLite برای توسعه/نصب‌های کوچک باقی مانده است. مسیرهای عملیاتی مهم:

- `scripts/install_from_github.sh`, `scripts/install.sh`
- `scripts/update_from_github.sh`
- `scripts/backup.sh`, `scripts/restore.sh`
- `scripts/doctor.sh`
- systemd/nginx templates در `scripts/templates`
- Dockerfile و tenant deployment services

## نکته معماری برای آینده

`store/models.py` بسیار بزرگ و چنددامنه‌ای است. برای سرعت فعلی توسعه قابل قبول بوده، اما اگر آزادنت بخواهد تیمی و بلندمدت روی آن کار کند، بهتر است در roadmap فازبندی bounded contextها در نظر گرفته شود: sales، provisioning، bot، admin ops، growth، deployment.

