from django import forms
from django.contrib import admin, messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from store.admin_access import ensure_admin_capability, require_admin_capability, user_has_capability
from store.admin_revenue import get_store_options
from store.models import Store
from .models import OutreachEvent, OutreachSettings
from .outreach import LABELS, REASONS


class OutreachForm(forms.ModelForm):
    class Meta:
        model = OutreachSettings
        fields = ("mode", "inactivity_enabled", "renewal_enabled", "daily_limit", "start_hour", "end_hour")
        labels = {"mode": "وضعیت ارسال", "inactivity_enabled": "پیگیری ۴۸ ساعت عدم مصرف", "renewal_enabled": "هشدار حجم، انقضا و تمدید",
                  "daily_limit": "حداکثر پیام روزانه فروشگاه", "start_hour": "شروع ارسال به وقت تهران", "end_hour": "پایان ارسال به وقت تهران"}


@require_http_methods(["GET", "POST"])
@require_admin_capability("revenue.view")
def outreach_dashboard(request):
    stores, store = get_store_options(request.GET.get("store"))
    if request.method == "POST":
        if not str(request.POST.get("store", "")).isdigit():
            raise Http404
        store = get_object_or_404(Store, pk=request.POST.get("store"))
        action = request.POST.get("action", "save")
        if action == "retry":
            ensure_admin_capability(request.user, "revenue.enable_real_send")
            if not str(request.POST.get("event", "")).isdigit():
                raise Http404
            event = get_object_or_404(OutreachEvent, pk=request.POST.get("event"), order__store=store, status="failed")
            event.status, event.reason = "ready", ""
            event.claimed_at = None
            event.save(update_fields=["status", "reason", "claimed_at", "updated_at"])
            messages.success(request, "برای بررسی مجدد شرایط و تلاش بعدی در صف قرار گرفت.")
            return redirect(f"{request.path}?store={store.pk}")
        ensure_admin_capability(request.user, "revenue.enable_real_send" if request.POST.get("mode") == "live" else "revenue.manage_safe")
        config, _ = OutreachSettings.objects.get_or_create(store=store)
        form = OutreachForm(request.POST, instance=config)
        if form.is_valid():
            config = form.save(commit=False)
            config.changed_by = request.user
            if config.mode == "live" and not config.activated_at:
                config.activated_at = timezone.now()
            config.save()
            messages.success(request, "تنظیمات پیام‌های هر خرید ذخیره شد.")
            return redirect(f"{request.path}?store={store.pk}")
    else:
        config = OutreachSettings.objects.filter(store=store).first() if store else None
        form = OutreachForm(instance=config or OutreachSettings(store=store))
    rows = []
    if store:
        query = OutreachEvent.objects.filter(order__store=store).select_related("order__customer").order_by("-updated_at", "-pk")
        selected = request.GET.get("status", "")
        if selected in OutreachEvent.Status.values:
            query = query.filter(status=selected)
        for event in query[:100]:
            rows.append({"event": event, "kind": LABELS.get(event.kind, event.kind), "reason": REASONS.get(event.reason, event.reason)})
    return TemplateResponse(request, "admin/store/customers/outreach.html", {
        **admin.site.each_context(request), "title": "پیام‌های هوشمند هر خرید", "stores": stores, "selected_store": store,
        "form": form, "rows": rows, "config": config, "statuses": OutreachEvent.Status.choices,
        "can_manage": user_has_capability(request.user, "revenue.manage_safe"),
        "can_send": user_has_capability(request.user, "revenue.enable_real_send"),
    })
