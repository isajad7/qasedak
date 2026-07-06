import os

from django.db import connection
from django.http import JsonResponse

from store.models import BotConfiguration


def health(request):
    database = {"reachable": False}
    bot = {
        "configured": False,
        "runtime_token": "missing",
        "active_configs": None,
        "api_check": "skipped",
    }

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        database["reachable"] = True
    except Exception as exc:
        database["error"] = exc.__class__.__name__

    if database["reachable"]:
        try:
            runtime_token = bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip())
            configs = BotConfiguration.objects.filter(
                provider=BotConfiguration.Provider.TELEGRAM,
                is_active=True,
            )
            if not runtime_token:
                configs = configs.exclude(bot_token="")
            active_configs = configs.count()
            bot.update(
                {
                    "configured": runtime_token or active_configs > 0,
                    "runtime_token": "set" if runtime_token else "missing",
                    "active_configs": active_configs,
                }
            )
        except Exception as exc:
            bot["error"] = exc.__class__.__name__

    status = 200 if database["reachable"] else 503
    return JsonResponse(
        {
            "service": "alive",
            "tenant_id": os.environ.get("TENANT_ID", ""),
            "database": database,
            "bot": bot,
        },
        status=status,
    )
