#!/usr/bin/env bash
set -Eeuo pipefail
source /etc/qasedak/dev-deploy.conf
[[ "$(cat "$QASEDAK_DEV_ROOT/.qasedak-development-target")" == 'isajad7/qasedak:development' ]]
app="$(docker ps --filter "label=com.docker.compose.project=$QASEDAK_DEV_PROJECT" --filter "label=com.docker.compose.service=$QASEDAK_DEV_SERVICE" --format '{{.ID}}')"
[[ "$app" =~ ^[0-9a-f]{12,64}$ ]]
[[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$app")" == "$QASEDAK_DEV_SERVICE" ]]
if [[ "${1:-}" == "--origin" ]]; then
docker exec -i "$app" python manage.py shell --no-imports <<'ORIGIN'
import json
from urllib.parse import urlsplit
from store.models import Panel
for panel in Panel.objects.filter(family='pasarguard', is_active=True).order_by('pk'):
    try:
        parts = urlsplit(panel.url)
        host = parts.hostname
        if parts.scheme not in {'http', 'https'} or not host: continue
        netloc = f'[{host}]' if ':' in host else host
        if parts.port: netloc += f':{parts.port}'
        origin = parts._replace(netloc=netloc, query='', fragment='').geturl()
        print(json.dumps({'panel_id': panel.pk, 'origin': origin}), flush=True)
    except ValueError:
        continue
ORIGIN
exit 0
fi



docker exec -i "$app" python manage.py shell --no-imports <<'PY'
import json
from store.models import Order,BotEventLog,BotUser
for order in Order.objects.filter(pk__in=[1135,1136]).order_by('pk'):
    meta=order.metadata or {}
    logs=BotEventLog.objects.filter(order=order).order_by('-created_at')[:8]
    print(json.dumps({'order_id':order.pk,'status':order.status,'provisioning_status':order.provisioning_status,'customer_suppressed':bool(meta.get('suppress_customer_notification')),'admin_suppressed':bool(meta.get('suppress_admin_order_updates')),'active_bot_targets':BotUser.objects.filter(customer_id=order.customer_id,is_active=True,bot_config__is_active=True).exclude(chat_id='').count(),'customer_notification_failures_since_recovery':BotEventLog.objects.filter(order=order,event_type='error',status='failed',created_at__gte='2026-10-04T14:07:00Z',message__startswith='Could not notify customer').count()}),flush=True)
PY
