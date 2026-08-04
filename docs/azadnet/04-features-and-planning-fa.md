# موجودی فیچرها و پلنینگ آینده

آخرین بررسی: 2026-08-04

## وضعیت کلی

قاصدک از نظر دامنه محصول بسیار فراتر از MVP است. بخش‌های فروش، ربات، ادمین، payment review، X-UI/Sanaei provisioning، گزارش‌ها، کمپین، Revenue Engine، بکاپ و مسیر نصب production پیاده‌سازی شده‌اند. همزمان چند بخش نیاز به تصمیم یا توسعه تکمیلی دارند: license، Marzban، payment gateway، observability استاندارد، جداسازی دامنه‌ها و مستندسازی APIهای tenant.

## موجودی فیچرها

| دامنه | وضعیت | توضیح |
| --- | --- | --- |
| فروش وب | پیاده‌سازی‌شده | صفحه خرید، اپراتور، پلن، حجم دلخواه، رسید تصویری، discount، referral |
| داشبورد مشتری | پیاده‌سازی‌شده | سفارش‌ها، سرویس‌ها، مصرف، تمدید، پاداش referral، اتصال تلگرام |
| ربات فروش | پیاده‌سازی‌شده | خرید، تمدید، رسید، سفارش‌ها، سرویس‌ها، مصرف، support، trial، referral |
| ادمین داخل ربات | پیاده‌سازی‌شده | سفارش‌های pending، گزارش فروش، broadcast، config management، support |
| پرداخت دستی | پیاده‌سازی‌شده | کارت‌به‌کارت، تصویر رسید، متن رسید، تحلیل مبلغ، زمان پرداخت |
| SMSForwarder | پیاده‌سازی‌شده | webhook امن، parse پیامک فارسی، match مبلغ/زمان، notification |
| Payment gateway | ناقص/آینده | فیلدهای مدل وجود دارد، ولی flow عملیاتی gateway در بررسی کد دیده نشد |
| X-UI/Sanaei | پیاده‌سازی‌شده | create, enable, renew, delete, usage, sync, health, compatibility |
| 3X-UI modern multi-node | پیاده‌سازی‌شده با گارد | deferred provisioning، scope freeze، capability audit، multi-inbound bundle |
| Marzban | placeholder امن | adapter وجود دارد ولی عملیات remote عمدا unsupported است |
| Plan routing | پیاده‌سازی‌شده | route پلن به inbound، operator-specific، fallback کنترل‌شده، bulk و panel center |
| Subscription Cup | در حال اضافه شدن/پیاده‌سازی‌شده در تغییرات فعلی | مدل و rebuild command در worktree جدید دیده می‌شود |
| Free trial | پیاده‌سازی‌شده | تنظیمات store، cooldown، lock، ساخت کانفیگ کوتاه‌مدت |
| Referral | پیاده‌سازی‌شده | کد/لینک دعوت، ledger جایزه، redeem روی کانفیگ فعال |
| Support | پیاده‌سازی‌شده | وب، ربات، workbench ادمین، پاسخ و close/reopen |
| Broadcast campaigns | پیاده‌سازی‌شده | draft، audience، preview، queue، processor، safe export |
| Revenue Engine | پیاده‌سازی‌شده با dry-run | renewal/upsell/retention، guardrails، logs، A/B، AI optimizer |
| Renewal reminders | پیاده‌سازی‌شده | قبل/بعد انقضا و low traffic با cooldown و logs |
| Reports Center | پیاده‌سازی‌شده | فروش، مشتری، سرویس، عملیات، revenue، panel usage و CSV امن |
| Panel health/usage | پیاده‌سازی‌شده | health status، check logs، usage snapshots، daily usage |
| Service reconciliation | پیاده‌سازی‌شده | read-only remote check، local soft-delete امن |
| Staff roles | پیاده‌سازی‌شده | نقش‌های محصولی و capability map |
| Backup/Restore | پیاده‌سازی‌شده | jobs، package validation، restore command، scripts |
| Installer/Doctor | پیاده‌سازی‌شده | install/update/uninstall، PostgreSQL/SQLite، systemd/nginx، doctor |
| Tenant/orchestrator | پیاده‌سازی‌شده/در مسیر محصول‌سازی | ServerNode، TenantInstance، Docker deployment، owner toolkit |
| License | تصمیم باز | `LICENSE` وجود ندارد و سند productization انتخاب license را TODO گذاشته است |

## فیچرهای ممتاز برای آزادنت

- ربات فروش کامل با state machine و عملیات ادمین
- deferred provisioning برای 3X-UI مدرن و جلوگیری از ساخت کانفیگ قبل از تایید پرداخت
- route دقیق پلن به inbound و پشتیبانی multi-inbound bundle
- جداسازی verification و provisioning در Order
- Revenue Engine با dry-run و guardrail، مناسب آزمایش رشد بدون ریسک ارسال گسترده
- Service reconciliation با local soft-delete و بدون remote mutation
- پنل ادمین workflow محور، نه صرفا CRUD خام
- Staff roles محصولی برای تیم عملیاتی
- بکاپ/ریستور و مسیر نصب production
- نشانه‌های SaaS readiness و tenant orchestration

## محدودیت‌ها و تصمیم‌های باز

### 1. License

برای انتشار عمومی یا استفاده رسمی در اکوسیستم آزادنت باید license انتخاب شود. گزینه‌ها در `docs/productization/06-license-decision.md` آمده‌اند. بدون license، وضعیت حقوقی استفاده بیرونی روشن نیست.

### 2. Marzban

مدل `Panel.Family` و adapter factory آماده‌اند، اما Marzban هنوز پیاده‌سازی عملیاتی ندارد. برای تبدیل آن به فیچر واقعی باید API، auth، inbound/user model، ساخت/تمدید/حذف، usage و تست‌های live-safe اضافه شوند.

### 3. Payment gateway

مدل Order فیلدهای gateway دارد، اما flow کامل در کد بررسی‌شده دیده نشد. اگر آزادنت به پرداخت آنلاین نیاز دارد، این یک epic جدا است: provider، callback، verify، idempotency، reconcile و گزارش مالی.

### 4. دامنه بزرگ `store`

بخش زیادی از پروژه در app واحد `store` و مدل بزرگ `store/models.py` جمع شده است. برای سرعت توسعه خوب بوده، اما برای رشد تیمی بهتر است به مرور دامنه‌ها جدا شوند.

پیشنهاد bounded context:

- `sales`: Plan، Order، Discount، Checkout
- `provisioning`: Panel، Inbound، VPNClient، X-UI adapters
- `bot`: BotConfiguration، BotUser، flows
- `support`: Conversation/Message
- `growth`: Referral، Campaign، Revenue Engine
- `ops`: Backup، Restore، Reports، Tenant

### 5. Observability

لاگ‌های زیادی وجود دارد، اما یک پیکربندی متمرکز LOGGING، metrics و tracing رسمی در کد بررسی‌شده برجسته نبود. برای production چند tenant، لاگ ساخت‌یافته و alerting باید جدی‌تر شود.

### 6. مستندسازی APIهای tenant

APIهای `/orchestrator/v2/*` وجود دارند، اما برای استفاده اکوسیستمی بهتر است schema، auth model، payload نمونه و failure modes به صورت مستقل مستند شوند.

### 7. تست live integration

بسیاری از مسیرها عمدا live check را optional نگه می‌دارند. برای release هر tenant باید checklist مشخص live-bot، live-xui، route test، webhook SMS و backup rehearsal وجود داشته باشد.

## پیشنهاد پلن فازبندی

### فاز 1 - تثبیت دانش و release داخلی

- همین مستندات را به عنوان baseline تیمی تایید کنید.
- نام رسمی محصول و نسبت با آزادنت را نهایی کنید.
- license را انتخاب کنید.
- checklist release داخلی بسازید: install، setup wizard، bot purchase، admin approval، delivery، renewal، backup.
- تغییرات فعلی worktree را review و commit کنید.

### فاز 2 - سخت‌سازی production

- LOGGING structured و redaction مرکزی را تکمیل کنید.
- health/doctor را به runbook رسمی tenantها وصل کنید.
- PostgreSQL rehearsal واقعی با دیتای مشابه production انجام دهید.
- dashboardهای خطا برای provisioning، SMS، Telegram و X-UI بسازید.
- تست‌های smoke برای مسیرهای critical خرید/تایید/تحویل اضافه کنید.

### فاز 3 - گسترش اکوسیستم

- Marzban را به عنوان adapter واقعی پیاده‌سازی کنید یا رسما از scope خارج کنید.
- payment gateway را طراحی و اضافه کنید.
- Orchestrator API را مستند و versioned کنید.
- tenant lifecycle را با backup، restore، suspend/resume و bot worker به runbook تبدیل کنید.
- مدل domain را مرحله‌ای refactor کنید تا توسعه تیمی آسان‌تر شود.

### فاز 4 - رشد هوشمند

- Revenue Engine را با dry-run داده‌محور validate کنید.
- target coverage و conversion tracking را قبل از real-send بسنجید.
- A/B و AI optimizer را با قوانین محافظه‌کارانه و سقف‌های کوچک rollout کنید.
- کمپین‌ها و Revenue Engine را از نظر overlap، cooldown و تجربه مشتری هماهنگ کنید.

## معیارهای آمادگی برای استفاده در آزادنت

یک نصب قاصدک وقتی برای فروش واقعی آماده است که:

- Store فعال و `setup_status=ready` باشد
- Telegram bot token، admin IDs و username تنظیم شده باشد
- حداقل یک Panel X-UI/Sanaei فعال و sync شده باشد
- حداقل یک Inbound فعال و sellable وجود داشته باشد
- برای هر Plan عمومی route معتبر وجود داشته باشد
- پرداخت کارت‌به‌کارت واقعی و SMS webhook در صورت نیاز تنظیم باشد
- خرید آزمایشی از وب یا ربات تا تحویل کانفیگ کامل شود
- Revenue Engine هنوز dry-run باشد مگر rollout رسمی انجام شده باشد
- backup قبل از launch گرفته و validate شده باشد
- حداقل یک superuser و نقش‌های staff لازم تعریف شده باشند
- doctor و live checks انتخابی بدون خطای بحرانی اجرا شده باشند

## ریسک‌های عملیاتی مهم

| ریسک | اثر | کنترل پیشنهادی |
| --- | --- | --- |
| route ناقص یا inbound غیرفعال | سفارش ثبت می‌شود ولی provisioning شکست می‌خورد | Setup checklist، route audit، Panel Center |
| اشتباه در profile پنل 3X-UI | ساخت روی scope غلط یا failure | compatibility audit و frozen scope |
| ارسال real Revenue Engine زودهنگام | پیام ناخواسته به کاربران | dry-run، caps، canary، validate targets |
| SMS match چندتایی | تایید پرداخت روی سفارش اشتباه | انتخاب دستی برای چند match و audit |
| بکاپ بدون rehearsal | ریسک بازیابی در بحران | validate restore و runbook |
| نبود license | ابهام حقوقی انتشار | تصمیم رسمی قبل از public release |
| app بزرگ و coupling زیاد | کندی توسعه آینده | refactor مرحله‌ای با حفظ API/رفتار |

## خروجی پیشنهادی برای پلنینگ آزادنت

برای هر roadmap بعدی، قاصدک را به 5 epic نگاه کنید:

1. Sales Core: checkout، payment، order، provisioning
2. Bot Commerce: تجربه کامل خرید و تمدید در Telegram/Bale
3. Admin Operations: workbenchها، staff، reports، support
4. Infrastructure Integrations: X-UI/Sanaei، Marzban آینده، tenant deployment، backup
5. Growth Engine: referral، trial، campaigns، revenue/retention/upsell

این تقسیم‌بندی کمک می‌کند هر تصمیم محصولی مشخص کند به کدام بخش فشار می‌آورد و چه تست/مستندی لازم دارد.

