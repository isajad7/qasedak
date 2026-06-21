from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from .models import BotConfiguration, Inbound, Panel, Plan, PlanInboundRoute, Store, normalize_payment_digits


SECRET_PLACEHOLDER = "•••••••• مقدار قبلی حفظ می‌شود"
SAFE_PLACEHOLDER_CARD_NUMBER = "0000000000000000"
SAFE_PLACEHOLDER_CARD_OWNER = "Configure Payment Owner"


def _password_widget(placeholder=SECRET_PLACEHOLDER):
    return forms.PasswordInput(render_value=False, attrs={"placeholder": placeholder, "autocomplete": "new-password"})


class StoreIdentitySetupForm(forms.Form):
    name = forms.CharField(label=_("نام فروشگاه"), max_length=100)
    english_name = forms.CharField(label=_("نام انگلیسی"), max_length=100)
    domain = forms.CharField(label=_("دامنه"), max_length=255, required=False)

    def __init__(self, *args, store=None, **kwargs):
        self.store = store
        initial = kwargs.pop("initial", {})
        if store:
            initial = {
                **initial,
                "name": store.name,
                "english_name": store.english_name,
                "domain": store.domain or "",
            }
        super().__init__(*args, initial=initial, **kwargs)

    def save(self):
        store = self.store or Store()
        for field in ("name", "english_name", "domain"):
            setattr(store, field, self.cleaned_data[field])
        if not store.card_number:
            store.card_number = SAFE_PLACEHOLDER_CARD_NUMBER
        if not store.card_owner:
            store.card_owner = SAFE_PLACEHOLDER_CARD_OWNER
        store.save()
        self.store = store
        return store


class PaymentSetupForm(forms.Form):
    card_owner = forms.CharField(label=_("صاحب کارت"), max_length=100)
    card_number = forms.CharField(
        label=_("شماره کارت"),
        required=False,
        strip=True,
        widget=_password_widget(_("شماره جدید را وارد کن؛ خالی یعنی مقدار قبلی حفظ شود.")),
    )
    bank_name = forms.CharField(label=_("نام بانک"), max_length=100, required=False)
    sheba_number = forms.CharField(label=_("شماره شبا"), max_length=34, required=False)
    receipt_image_only_payment = forms.BooleanField(label=_("فقط عکس رسید دریافت شود"), required=False)
    payment_sms_time_zone = forms.CharField(label=_("منطقه زمانی SMS پرداخت"), max_length=64, required=False)
    smsforwarder_token = forms.CharField(
        label=_("SMSForwarder webhook token"),
        required=False,
        strip=True,
        widget=_password_widget(),
        help_text=_("اگر خالی بماند، token قبلی پاک نمی‌شود."),
    )

    def __init__(self, *args, store=None, **kwargs):
        self.store = store
        initial = kwargs.pop("initial", {})
        if store:
            initial = {
                **initial,
                "card_owner": store.card_owner,
                "bank_name": store.bank_name or "",
                "sheba_number": store.sheba_number or "",
                "receipt_image_only_payment": store.receipt_image_only_payment,
                "payment_sms_time_zone": store.payment_sms_time_zone or "Asia/Tehran",
            }
        super().__init__(*args, initial=initial, **kwargs)
        if store and store.card_number:
            self.fields["card_number"].widget.attrs["placeholder"] = _(
                "شماره کارت ذخیره شده؛ برای تغییر مقدار جدید را وارد کن."
            )

    def clean_card_number(self):
        value = normalize_payment_digits(self.cleaned_data.get("card_number") or "").strip()
        if value:
            if not value.isdigit() or len(value) != 16:
                raise ValidationError(_("شماره کارت باید ۱۶ رقم باشد."))
            return value
        if self.store and self.store.card_number:
            return self.store.card_number
        raise ValidationError(_("شماره کارت برای پرداخت کارت‌به‌کارت لازم است."))

    def save(self):
        if not self.store:
            raise ValidationError(_("ابتدا مرحله هویت فروشگاه را کامل کن."))
        store = self.store
        for field in (
            "card_owner",
            "card_number",
            "bank_name",
            "sheba_number",
            "receipt_image_only_payment",
            "payment_sms_time_zone",
        ):
            setattr(store, field, self.cleaned_data[field])
        token = self.cleaned_data.get("smsforwarder_token")
        if token:
            store.set_smsforwarder_webhook_token(token)
        store.save()
        return store


class TelegramSetupForm(forms.Form):
    is_active = forms.BooleanField(label=_("ربات فعال باشد"), required=False, initial=True)
    name = forms.CharField(label=_("نام تنظیمات ربات"), max_length=100, required=False)
    telegram_bot_username = forms.CharField(label=_("نام کاربری ربات تلگرام"), max_length=64, required=False)
    bot_token = forms.CharField(
        label=_("Bot token"),
        required=False,
        strip=True,
        widget=_password_widget(),
        help_text=_("token کامل بعد از ذخیره نمایش داده نمی‌شود. خالی یعنی مقدار قبلی حفظ شود."),
    )
    admin_user_id = forms.CharField(label=_("Admin user ID اصلی"), max_length=80, required=False)
    additional_admin_user_ids = forms.CharField(
        label=_("Admin user IDهای اضافه"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def __init__(self, *args, store=None, bot_config=None, **kwargs):
        self.store = store
        self.bot_config = bot_config
        initial = kwargs.pop("initial", {})
        if bot_config:
            initial = {
                **initial,
                "is_active": bot_config.is_active,
                "name": bot_config.name,
                "telegram_bot_username": bot_config.telegram_bot_username,
                "admin_user_id": bot_config.admin_user_id,
                "additional_admin_user_ids": bot_config.additional_admin_user_ids,
            }
        else:
            initial = {**initial, "is_active": True, "name": "Telegram bot"}
        super().__init__(*args, initial=initial, **kwargs)

    def clean_bot_token(self):
        value = (self.cleaned_data.get("bot_token") or "").strip()
        if value:
            return value
        if self.bot_config and self.bot_config.bot_token:
            return self.bot_config.bot_token
        return ""

    def save(self):
        config = self.bot_config or BotConfiguration(provider=BotConfiguration.Provider.TELEGRAM, store=self.store)
        config.provider = BotConfiguration.Provider.TELEGRAM
        config.store = self.store
        config.is_active = self.cleaned_data["is_active"]
        config.name = self.cleaned_data.get("name") or "Telegram bot"
        config.telegram_bot_username = self.cleaned_data.get("telegram_bot_username") or ""
        config.bot_token = self.cleaned_data.get("bot_token") or config.bot_token or ""
        config.admin_user_id = self.cleaned_data.get("admin_user_id") or ""
        config.additional_admin_user_ids = self.cleaned_data.get("additional_admin_user_ids") or ""
        config.clean()
        config.save()
        self.bot_config = config
        return config


class TelegramProxySetupForm(forms.Form):
    proxy_enabled = forms.BooleanField(label=_("Proxy تلگرام لازم است"), required=False)
    proxy_url = forms.CharField(
        label=_("Proxy URL"),
        required=False,
        widget=_password_widget(_("Proxy از env/settings خوانده می‌شود؛ مقدار کامل نمایش داده نمی‌شود.")),
        help_text=_("در این نسخه wizard مقدار env را تغییر نمی‌دهد. برای تست، از command جداگانه استفاده کن."),
    )

    def save(self):
        return None


class PanelSetupForm(forms.Form):
    name = forms.CharField(label=_("نام پنل"), max_length=100)
    url = forms.URLField(label=_("URL پنل"))
    username = forms.CharField(label=_("Username"), max_length=100)
    password = forms.CharField(
        label=_("Password"),
        required=False,
        strip=True,
        widget=_password_widget(),
        help_text=_("اگر خالی بماند password قبلی حفظ می‌شود."),
    )
    proxy_url = forms.URLField(
        label=_("Proxy URL اختیاری پنل"),
        required=False,
        widget=_password_widget(_("Proxy جدید را وارد کن؛ خالی یعنی مقدار قبلی حفظ شود.")),
    )
    clear_proxy_url = forms.BooleanField(label=_("Proxy پنل حذف شود"), required=False)
    is_active = forms.BooleanField(label=_("پنل فعال باشد"), required=False, initial=True)

    def __init__(self, *args, store=None, panel=None, **kwargs):
        self.store = store
        self.panel = panel
        initial = kwargs.pop("initial", {})
        if panel:
            initial = {
                **initial,
                "name": panel.name,
                "url": panel.url,
                "username": panel.username,
                "is_active": panel.is_active,
            }
        else:
            initial = {**initial, "is_active": True, "name": "Primary X-UI panel"}
        super().__init__(*args, initial=initial, **kwargs)

    def clean_password(self):
        value = (self.cleaned_data.get("password") or "").strip()
        if value:
            return value
        if self.panel and self.panel.password:
            return self.panel.password
        raise ValidationError(_("Password برای پنل جدید لازم است."))

    def clean_proxy_url(self):
        value = (self.cleaned_data.get("proxy_url") or "").strip()
        if value:
            return value
        if self.panel:
            return self.panel.proxy_url or ""
        return ""

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("clear_proxy_url"):
            cleaned_data["proxy_url"] = ""
        return cleaned_data

    def save(self):
        panel = self.panel or Panel(store=self.store)
        panel.store = self.store
        panel.name = self.cleaned_data["name"]
        panel.url = self.cleaned_data["url"]
        panel.username = self.cleaned_data["username"]
        panel.password = self.cleaned_data["password"]
        panel.proxy_url = self.cleaned_data["proxy_url"] or None
        panel.is_active = self.cleaned_data["is_active"]
        panel.full_clean()
        panel.save()
        self.panel = panel
        return panel


class InboundSetupForm(forms.Form):
    panel = forms.ModelChoiceField(label=_("پنل"), queryset=Panel.objects.none())
    inbound_id = forms.IntegerField(label=_("X-UI inbound ID"), min_value=1)
    remark = forms.CharField(label=_("عنوان/Remark"), max_length=150, required=False)
    server_ip = forms.CharField(label=_("Server IP/host"), max_length=100)
    port = forms.CharField(label=_("Port"), max_length=10)
    config_params = forms.CharField(label=_("پارامترهای کانفیگ"), max_length=500, initial="type=tcp&security=none")
    is_active = forms.BooleanField(label=_("فعال باشد"), required=False, initial=True)
    available_for_new_orders = forms.BooleanField(label=_("برای سفارش جدید استفاده شود"), required=False, initial=True)
    health_monitor_enabled = forms.BooleanField(label=_("در health monitor باشد"), required=False, initial=True)

    def __init__(self, *args, store=None, inbound=None, **kwargs):
        self.store = store
        self.inbound = inbound
        initial = kwargs.pop("initial", {})
        panel_queryset = Panel.objects.filter(is_active=True).order_by("name", "pk")
        if store:
            panel_queryset = panel_queryset.filter(store=store)
        if inbound:
            initial = {
                **initial,
                "panel": inbound.panel_id,
                "inbound_id": inbound.inbound_id,
                "remark": inbound.remark,
                "server_ip": inbound.server_ip,
                "port": inbound.port,
                "config_params": inbound.config_params,
                "is_active": inbound.is_active,
                "available_for_new_orders": inbound.available_for_new_orders,
                "health_monitor_enabled": inbound.health_monitor_enabled,
            }
        super().__init__(*args, initial=initial, **kwargs)
        self.fields["panel"].queryset = panel_queryset

    def save(self):
        inbound = self.inbound or Inbound()
        for field in (
            "panel",
            "inbound_id",
            "remark",
            "server_ip",
            "port",
            "config_params",
            "is_active",
            "available_for_new_orders",
            "health_monitor_enabled",
        ):
            setattr(inbound, field, self.cleaned_data[field])
        inbound.full_clean()
        inbound.save()
        self.inbound = inbound
        return inbound


class PlanSetupForm(forms.Form):
    name = forms.CharField(label=_("نام پلن"), max_length=100)
    volume_gb = forms.DecimalField(label=_("حجم GB"), min_value=0)
    duration_days = forms.IntegerField(label=_("مدت روز"), min_value=1)
    price = forms.IntegerField(label=_("قیمت"), min_value=0)
    currency = forms.ChoiceField(label=_("واحد پول"), choices=Plan.Currency.choices, initial=Plan.Currency.TOMAN)
    is_active = forms.BooleanField(label=_("فعال باشد"), required=False, initial=True)
    is_public = forms.BooleanField(label=_("در فروش عمومی نمایش داده شود"), required=False, initial=True)

    def __init__(self, *args, store=None, plan=None, **kwargs):
        self.store = store
        self.plan = plan
        initial = kwargs.pop("initial", {})
        if plan:
            initial = {
                **initial,
                "name": plan.name,
                "volume_gb": plan.volume_gb,
                "duration_days": plan.duration_days,
                "price": plan.price,
                "currency": plan.currency,
                "is_active": plan.is_active,
                "is_public": plan.is_public,
            }
        else:
            initial = {
                **initial,
                "name": "Starter 30 days",
                "volume_gb": "1.000",
                "duration_days": 30,
                "currency": Plan.Currency.TOMAN,
                "is_active": True,
                "is_public": True,
            }
        super().__init__(*args, initial=initial, **kwargs)

    def save(self):
        plan = self.plan or Plan(store=self.store)
        plan.store = self.store
        for field in ("name", "volume_gb", "duration_days", "price", "currency", "is_active", "is_public"):
            setattr(plan, field, self.cleaned_data[field])
        if not plan.slug:
            plan.slug = ""
        plan.full_clean()
        plan.save()
        self.plan = plan
        return plan


class PlanRouteSetupForm(forms.Form):
    plan = forms.ModelChoiceField(label=_("پلن"), queryset=Plan.objects.none())
    inbound = forms.ModelChoiceField(label=_("Inbound مقصد"), queryset=Inbound.objects.none())
    is_active = forms.BooleanField(label=_("Route فعال باشد"), required=False, initial=True)
    priority = forms.IntegerField(label=_("اولویت"), min_value=0, initial=100)
    weight = forms.IntegerField(label=_("Weight"), min_value=1, initial=1)
    note = forms.CharField(label=_("یادداشت"), required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, store=None, route=None, **kwargs):
        self.store = store
        self.route = route
        initial = kwargs.pop("initial", {})
        plan_queryset = Plan.objects.filter(is_active=True, is_public=True, is_custom_volume=False).order_by("sort_order", "price", "pk")
        inbound_queryset = Inbound.objects.filter(is_active=True, available_for_new_orders=True, panel__is_active=True).select_related("panel")
        if store:
            plan_queryset = plan_queryset.filter(store=store)
            inbound_queryset = inbound_queryset.filter(panel__store=store)
        if route:
            initial = {
                **initial,
                "plan": route.plan_id,
                "inbound": route.inbound_id,
                "is_active": route.is_active,
                "priority": route.priority,
                "weight": route.weight,
                "note": route.note,
            }
        super().__init__(*args, initial=initial, **kwargs)
        self.fields["plan"].queryset = plan_queryset
        self.fields["inbound"].queryset = inbound_queryset

    def clean(self):
        cleaned_data = super().clean()
        plan = cleaned_data.get("plan")
        inbound = cleaned_data.get("inbound")
        if plan and not (plan.is_active and plan.is_public and not plan.is_custom_volume):
            self.add_error("plan", _("پلن باید فعال و قابل فروش عمومی باشد."))
        if inbound and not (inbound.is_active and inbound.available_for_new_orders and inbound.panel.is_active):
            self.add_error("inbound", _("Inbound باید فعال، قابل فروش و متصل به پنل فعال باشد."))
        return cleaned_data

    def save(self):
        route = self.route or PlanInboundRoute(store=self.store)
        route.store = self.store
        for field in ("plan", "inbound", "is_active", "priority", "weight", "note"):
            setattr(route, field, self.cleaned_data[field])
        route.full_clean()
        route.save()
        self.route = route
        return route
