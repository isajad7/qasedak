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
import json, ssl, time, socket
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



def summarize_error(exc):
    names=set()
    stack=[exc]
    seen=set()
    while stack:
        e=stack.pop()
        if id(e) in seen:continue
        seen.add(id(e));names.add(type(e).__name__)
        stack.extend(v for v in (getattr(e,'__cause__',None),getattr(e,'__context__',None),getattr(e,'reason',None),*getattr(e,'args',())) if isinstance(v,BaseException))
    return {'error_types':sorted(names),'error_code':getattr(exc,'error_code',''),'http_status':(getattr(exc,'safe_context',{}) or {}).get('status_code')}
for p in Panel.objects.filter(family='pasarguard',is_active=True).order_by('pk'):
    parts=urlsplit(p.url)
    try:
        with socket.create_connection((parts.hostname,parts.port or 443),timeout=3) as sock:
            with ssl.create_default_context().wrap_socket(sock,server_hostname=parts.hostname) as tls:
                print(json.dumps({'panel_id':p.pk,'default_negotiated_tls':tls.version()}),flush=True)
    except Exception as exc:print(json.dumps(summarize_error(exc)),flush=True)
    feed=ExternalSubscriptionFeed.objects.filter(panel=p,active=True,provider='pasarguard').exclude(status='disabled').first()
    for mode in ['shared','connection_close','fresh']:
        shared=requests.Session()
        shared.trust_env=False
        shared.mount('https://',PasarGuardHTTPSAdapter())
        if mode=='connection_close':shared.headers['Connection']='close'
        actions=['api','simple_groups','api_repeat']+(['subscription'] if feed else [])
        for action in actions:
            session=shared
            if mode=='fresh':
                session=requests.Session();session.trust_env=False;session.mount('https://',PasarGuardHTTPSAdapter())
            c=PasarGuardClient(p,session=session,timeout=(3,5))
            start=time.monotonic()
            result={'panel_id':p.pk,'mode':mode,'action':action}
            try:
                if action in ['api','api_repeat']:payload=c.get_system()
                elif action=='simple_groups':payload=c.list_groups_simple()
                else:payload=c.fetch_native_links(feed.protected_subscription_url)
                result['ok']=True
                if action not in ['api','api_repeat']:result['count']=len(payload)
            except Exception as exc:
                result.update(ok=False,**summarize_error(exc))
            result['elapsed_ms']=round((time.monotonic()-start)*1000)
            print(json.dumps(result),flush=True)
            if mode=='fresh':session.close()
        shared.close()
PY
