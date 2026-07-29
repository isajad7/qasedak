from django import forms
from django.utils.translation import gettext_lazy as _

from store.models import Panel


class PanelCenterForm(forms.ModelForm):
    password = forms.CharField(
        label=_("رمز عبور یا کلید دسترسی"),
        required=False,
        strip=True,
        widget=forms.PasswordInput(render_value=False),
        help_text=_("برای حفظ مقدار قبلی در حالت ویرایش، این فیلد را خالی بگذارید."),
    )
    proxy_url = forms.URLField(
        label=_("Proxy اختیاری"),
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text=_("در صورت نیاز به proxy، URL کامل را وارد کنید. مقدار ذخیره‌شده نمایش داده نمی‌شود."),
    )

    class Meta:
        model = Panel
        fields = ("name", "family", "url", "username", "password", "proxy_url", "is_active")
        labels = {
            "name": _("نام پنل"),
            "family": _("خانواده پنل"),
            "url": _("آدرس پایه پنل"),
            "username": _("نام کاربری"),
            "is_active": _("فعال باشد"),
        }
        help_texts = {
            "family": _("برای 3X-UI/Sanaei گزینه X-UI را انتخاب کنید. Marzban فعلاً فقط به‌صورت امن و غیرعملیاتی نمایش داده می‌شود."),
            "url": _("آدرس کامل پنل بدون اسلش انتهایی. اطلاعات ورود داخل URL ذخیره نکنید."),
            "username": _("برای پنل‌هایی که نام کاربری ندارند، مقدار سازگار با API پنل را وارد کنید."),
        }

    def clean_url(self):
        return (self.cleaned_data.get("url") or "").strip().rstrip("/")

    def clean_password(self):
        password = (self.cleaned_data.get("password") or "").strip()
        if password:
            return password
        if self.instance and self.instance.pk:
            return self.instance.password
        raise forms.ValidationError(_("برای ساخت پنل جدید، رمز عبور یا کلید دسترسی لازم است."))

    def clean_proxy_url(self):
        proxy_url = (self.cleaned_data.get("proxy_url") or "").strip()
        if proxy_url:
            return proxy_url
        if self.instance and self.instance.pk:
            return self.instance.proxy_url
        return ""
