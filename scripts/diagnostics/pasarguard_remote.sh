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

# Compare the same credential-free request on the host and in the app container.
origins="$(mktemp /tmp/qasedak-panel-origins.XXXXXXXX)"
trap 'rm -f -- "$origins"' EXIT
docker exec -i "$app" python manage.py shell --no-imports <<'ORIGINS' > "$origins"
import json
from urllib.parse import urlsplit
from store.models import Panel
for p in Panel.objects.filter(family='pasarguard', is_active=True).order_by('pk'):
    parts = urlsplit(p.url)
    if parts.scheme == 'https' and parts.hostname:
        host = '[' + parts.hostname + ']' if ':' in parts.hostname else parts.hostname
        if parts.port: host += ':' + str(parts.port)
        print(json.dumps({'panel_id': p.pk, 'origin': parts._replace(netloc=host, query='', fragment='').geturl()}), flush=True)
ORIGINS
python3 - "$origins" <<'HOST'
import json, socket, subprocess, sys, time
from urllib.parse import urljoin, urlsplit
for line in open(sys.argv[1]):
    try: item=json.loads(line)
    except ValueError: continue
    if 'origin' not in item: continue
    parts=urlsplit(item['origin'])
    ips=sorted({a[4][0] for a in socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)})
    print(json.dumps({'panel_id':item['panel_id'],'location':'host','dns_addresses':ips}),flush=True)
    for mode, flags in [('default',[]),('tls12',['--tlsv1.2','--tls-max','1.2'])]:
        start=time.monotonic()
        cp=subprocess.run(['curl','--noproxy','*','--connect-timeout','3','--max-time','10','--http1.1','-s','-o','/dev/null','-w','%{http_code} %{time_connect} %{time_appconnect} %{time_starttransfer}',*flags,'-H','User-Agent: qasedak-connectivity-check',urljoin(item['origin'].rstrip('/')+'/', 'api/system')],capture_output=True,text=True)
        print(json.dumps({'panel_id':item['panel_id'],'location':'host','mode':mode,'returncode':cp.returncode,'timings':cp.stdout,'elapsed_ms':round((time.monotonic()-start)*1000)}),flush=True)
HOST
docker exec -i "$app" python manage.py shell --no-imports <<'CONTAINER'
import json, socket, ssl, time
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPSHandler
import requests
from store.models import Panel
for panel in Panel.objects.filter(family='pasarguard',is_active=True).order_by('pk'):
    print(json.dumps({'panel_id':panel.pk,'location':'container','configured_proxy':bool(panel.proxy_url),'environment_proxy':bool(requests.utils.get_environ_proxies(panel.url))}),flush=True)
    for mode in ['default','tls12']:
        ctx=ssl.create_default_context()
        if mode=='tls12':
            ctx.minimum_version=ssl.TLSVersion.TLSv1_2
            ctx.maximum_version=ssl.TLSVersion.TLSv1_2
        opener=build_opener(ProxyHandler({}),HTTPSHandler(context=ctx))
        start=time.monotonic()
        result={'panel_id':panel.pk,'location':'container','mode':mode}
        try:
            with opener.open(Request(urljoin(panel.url.rstrip('/')+'/', 'api/system'),headers={'User-Agent':'qasedak-connectivity-check'}),timeout=10) as response:
                result['http_status']=response.status
        except HTTPError as exc:
            result['http_status']=exc.code
            exc.close()
        except Exception as exc:
            result['error_type']=type(exc).__name__
            reason=getattr(exc,'reason',None)
            if isinstance(reason,BaseException):result['reason_type']=type(reason).__name__
        result['elapsed_ms']=round((time.monotonic()-start)*1000)
        print(json.dumps(result),flush=True)
CONTAINER

# Read-only authenticated validation using the proposed HTTPS adapter.
docker exec -i "$app" python manage.py shell --no-imports <<'AUTHENTICATED'
import json, ssl, time
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



for panel in Panel.objects.filter(family='pasarguard',is_active=True).order_by('pk'):
    with requests.Session() as session:
        session.trust_env=False
        session.mount('https://',PasarGuardHTTPSAdapter())
        client=PasarGuardClient(panel,session=session,timeout=(3,10))
        feed=ExternalSubscriptionFeed.objects.filter(panel=panel,active=True,provider='pasarguard').exclude(status='disabled').first()
        for action in ['api','groups'] + (['subscription'] if feed else []):
            start=time.monotonic()
            result={'panel_id':panel.pk,'mode':'verified_tls12','action':action}
            try:
                if action=='api':payload=client.get_system()
                elif action=='groups':payload=client.list_groups()
                else:payload=client.fetch_native_links(feed.protected_subscription_url)
                result['ok']=True
                if action!='api':result['count']=len(payload)
            except Exception as exc:
                result.update(ok=False,error_type=type(exc).__name__,error_code=getattr(exc,'error_code',''),http_status=(getattr(exc,'safe_context',{}) or {}).get('status_code'))
            result['elapsed_ms']=round((time.monotonic()-start)*1000)
            print(json.dumps(result),flush=True)
AUTHENTICATED
