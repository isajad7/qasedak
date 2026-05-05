from pathlib import Path

from django.conf import settings

from .models import Store


def get_static_version():
    output_css = Path(settings.BASE_DIR) / "static" / "css" / "output.css"
    try:
        return str(int(output_css.stat().st_mtime))
    except OSError:
        return "dev"


def store_info(request):
    current_customer = getattr(request, "customer", None)
    customer_has_orders = bool(
        current_customer and current_customer.orders.exists()
    )
    return {
        "store": Store.objects.first(),
        "static_version": get_static_version(),
        "current_customer": current_customer,
        "current_customer_has_orders": customer_has_orders,
    }
