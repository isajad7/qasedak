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
import json, ssl, time
from urllib.parse import urlsplit
import requests
from requests.adapters import HTTPAdapter
from store.models import Panel, ExternalSubscriptionFeed
from store.panels.pasarguard.client import PasarGuardClient
class PasarGuardHTTPSAdapter(HTTPAdapter):
    """Use verified TLS 1.2 on the production path that stalls with TLS 1.3."""

    @staticmethod
    def _tls_context():
        context = ssl.create_default_context(cafile=requests.certs.where())
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        return context

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["ssl_context"] = self._tls_context()
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        proxy_kwargs["ssl_context"] = self._tls_context()
        return super().proxy_manager_for(proxy, **proxy_kwargs)



for p in Panel.objects.filter(family='pasarguard',is_active=True).order_by('pk'):
    with requests.Session() as session:
        session.trust_env=False
        session.mount('https://',PasarGuardHTTPSAdapter())
        session.headers['Connection']='close'
        c=PasarGuardClient(p,session=session,timeout=(3,10))
        for action in ['api','groups']:
            result={'panel_id':p.pk,'action':action}
            try:
                payload=c.get_system() if action=='api' else c.list_groups()
                result['ok']=True
                if action=='groups':result['count']=len(payload)
            except Exception as exc:result.update(ok=False,error_type=type(exc).__name__,code=getattr(exc,'error_code',''),http_status=(getattr(exc,'safe_context',{}) or {}).get('status_code'))
            print(json.dumps(result),flush=True)
        for feed in ExternalSubscriptionFeed.objects.filter(panel=p,active=True,provider='pasarguard',vpn_client__isnull=False).exclude(status='disabled').select_related('vpn_client').order_by('pk')[:3]:
            vc=feed.vpn_client
            result={'panel_id':p.pk,'feed_id':feed.pk,'client_id':vc.pk}
            try:
                user=c.get_user(vc.xui_email or vc.username)
                upstream=user.get('subscription_url') or ''
                result.update(user_found=True,remote_url_matches_saved=upstream==feed.protected_subscription_url,url_present=bool(upstream))
                if upstream:
                    links=c.fetch_native_links(upstream)
                    result.update(ok=True,native_configs=len(links))
            except Exception as exc:result.update(ok=False,error_type=type(exc).__name__,code=getattr(exc,'error_code',''),http_status=(getattr(exc,'safe_context',{}) or {}).get('status_code'))
            print(json.dumps(result),flush=True)
PY

# Read the renewal target without changing quota, expiry, or remote accounts.
docker exec -i "$app" python manage.py shell --no-imports <<'RENEWAL'
import json
from store.models import Order, VPNClient
from store.xui_api import XUIService, sanitize_xui_operational_text
order=Order.objects.filter(pk=1134,order_tracking_code='73cc7eb4e7e94adfb69d4bdac7beb1c8').first()
if order:
    vc=VPNClient.objects.select_related('inbound__panel').filter(pk=(order.metadata or {}).get('renewal_client_pk')).first()
    result={'order_id':order.pk,'renewal_target_present':bool(vc),'provisioning_status':order.provisioning_status,'renewed_at_recorded':bool((order.metadata or {}).get('renewed_at'))}
    if vc and vc.inbound_id and vc.inbound.panel_id:
        panel=vc.inbound.panel
        result.update(client_id=vc.pk,panel_id=panel.pk,inbound_id=vc.inbound_id,store_matches=vc.store_id==order.store_id)
        try:
            data=XUIService(panel).get_inbound_clients(vc.inbound,use_cache=False)
            exact=[c for c in data if c.get('id')==str(vc.uuid)]
            email=[c for c in data if c.get('email')==(vc.xui_email or vc.username)]
            result.update(ok=True,remote_exact_uuid_matches=len(exact),remote_email_matches=len(email),remote_client_count=len(data))
            target=(exact or email or [None])[0]
            if target:result.update(remote_enabled=target.get('enable'),remote_expiry_ms=target.get('expiryTime'),remote_quota_bytes=target.get('totalGB'))
        except Exception as exc:
            result.update(ok=False,error_type=type(exc).__name__,error_code=getattr(exc,'error_code',''),safe_error=sanitize_xui_operational_text(exc,panel=panel,max_length=300))
    print(json.dumps(result),flush=True)
RENEWAL
