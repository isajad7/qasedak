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
from store.models import Order, PlanDeliverySource
from store import provisioning_services
from store.panels.pasarguard.adapter import PasarGuardPanelAdapter
from store.panels.pasarguard.schemas import normalize_pasarguard_username, pasarguard_note_marker
from store.panels.xui.adapter import XUIProvisioningRequest
for order in Order.objects.filter(pk__in=[1135,1136]).select_related('plan').order_by('pk'):
    sources=list(PlanDeliverySource.objects.filter(delivery_config__plan=order.plan,active=True).select_related('panel','inbound__panel').order_by('pk'))
    sources=[s for s in sources if s.inbound_id and (s.panel or s.inbound.panel).family=='pasarguard']
    print(json.dumps({'order_id':order.pk,'provisioning_status':order.provisioning_status,'source_count':len(sources)}),flush=True)
    if not sources: continue
    inbounds=[s.inbound for s in sources]; panel=sources[0].panel or inbounds[0].panel
    adapter=PasarGuardPanelAdapter(panel)
    identity=provisioning_services.multi_inbound_order_identity(order,inbounds,index=1)
    req=XUIProvisioningRequest(email_prefix=identity['email_prefix'],total_gb=order.plan.volume_gb,duration_days=order.plan.duration_days,inbound=inbounds[0] if len(inbounds)==1 else None,inbounds=inbounds,limit_ip=order.plan.device_limit,client_uuid=identity['uuid'],sub_id=identity['sub_id'],email=identity['email'])
    group_ids=[int(i.inbound_id) for i in inbounds]
    context=adapter._request_context(req,group_ids)
    username=normalize_pasarguard_username(req.email or req.email_prefix,context=context)
    for action in ['system','verify_groups','lookup_user']:
        try:
            if action=='system': result=adapter.client.get_system()
            elif action=='verify_groups': result=adapter._verify_groups(inbounds)
            else: result=adapter._get_user_or_none(username)
            print(json.dumps({'order_id':order.pk,'action':action,'ok':True,'user_present':bool(result) if action=='lookup_user' else None}),flush=True)
        except Exception as exc:
            detail=str(getattr(exc,'technical_detail',''))[:600].replace(username,'[order-user]')
            print(json.dumps({'order_id':order.pk,'action':action,'ok':False,'code':getattr(exc,'error_code',''),'detail':detail,'http_status':(getattr(exc,'safe_context',{}) or {}).get('status_code')}),flush=True)
PY
