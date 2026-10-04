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
docker exec -i "$app" python manage.py shell <<'PY'
import copy, json, socket, ssl, time
from urllib.parse import urlsplit, urljoin
import requests
from store.models import Panel, ExternalSubscriptionFeed
from store.panels.pasarguard.client import PasarGuardClient

def error(exc):
    seen, pending, names, numbers = set(), [exc], set(), set()
    while pending:
        item = pending.pop()
        if id(item) in seen: continue
        seen.add(id(item))
        names.add(type(item).__name__)
        errno = getattr(item, 'errno', None)
        if isinstance(errno, int): numbers.add(errno)
        pending.extend(value for value in (getattr(item, '__cause__', None), getattr(item, '__context__', None), getattr(item, 'reason', None), *getattr(item, 'args', ())) if isinstance(value, BaseException))
    return {'types': sorted(names), 'errno': sorted(numbers), 'code': getattr(exc, 'error_code', ''), 'http_status': (getattr(exc, 'safe_context', {}) or {}).get('status_code')}

def transport(url):
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    result = {'scheme': parts.scheme, 'port': port, 'families': []}
    try:
        addresses = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except Exception as exc:
        result['dns_error'] = error(exc)
        return result
    selected = {}
    for family, kind, protocol, _, address in addresses:
        selected.setdefault(family, (kind, protocol, address))
    for family, (kind, protocol, address) in selected.items():
        item = {'family': 'ipv6' if family == socket.AF_INET6 else 'ipv4'}
        try:
            with socket.socket(family, kind, protocol) as sock:
                sock.settimeout(3)
                sock.connect(address)
                item['tcp'] = True
                if parts.scheme == 'https':
                    sock.settimeout(5)
                    with ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname):
                        item['tls'] = True
        except Exception as exc:
            item['error'] = error(exc)
        result['families'].append(item)
    return result

for panel in Panel.objects.filter(family='pasarguard', is_active=True).order_by('pk'):
    feed = ExternalSubscriptionFeed.objects.filter(panel=panel, active=True, provider='pasarguard').exclude(status='disabled').order_by('pk').first()
    urls = {'api': panel.url}
    if feed: urls['subscription'] = feed.protected_subscription_url
    print(json.dumps({'panel_id': panel.pk, 'configured_proxy': bool(panel.proxy_url), 'environment_proxy': bool(requests.utils.get_environ_proxies(panel.url)), 'source_host_matches_panel': bool(feed and urlsplit(feed.protected_subscription_url).hostname == urlsplit(panel.url).hostname), 'transport': {name: transport(url) for name, url in urls.items()}}), flush=True)
    started = time.monotonic()
    unauthenticated = {'panel_id': panel.pk, 'location': 'production_server', 'action': 'unauthenticated_api'}
    try:
        with requests.Session() as probe:
            probe.trust_env = False
            with probe.get(urljoin(panel.url.rstrip('/') + '/', 'api/system'), headers={'User-Agent': 'qasedak-connectivity-check'}, timeout=(3, 12), stream=True) as response:
                unauthenticated['http_status'] = response.status_code
    except Exception as exc:
        unauthenticated['error'] = error(exc)
    unauthenticated['elapsed_ms'] = round((time.monotonic() - started) * 1000)
    print(json.dumps(unauthenticated), flush=True)
    modes = ['direct'] + (['environment'] if requests.utils.get_environ_proxies(panel.url) else []) + (['configured_proxy'] if panel.proxy_url else [])
    if urlsplit(panel.url).scheme == 'https' and urlsplit(panel.url).port == 8443:
        modes += ['https443']
    for mode in modes:
        with requests.Session() as session:
            session.trust_env = mode == 'environment'
            if mode == 'configured_proxy': session.proxies = {'http': panel.proxy_url, 'https': panel.proxy_url}
            target = copy.copy(panel)
            source = feed.protected_subscription_url if feed else ''
            if mode == 'https443':
                def on443(url):
                    parts = urlsplit(url)
                    host = parts.hostname
                    return parts._replace(netloc=f'[{host}]' if ':' in host else host).geturl()
                target.url = on443(panel.url)
                if feed and urlsplit(source).hostname == urlsplit(panel.url).hostname:
                    source = on443(source)
            client = PasarGuardClient(target, session=session, timeout=(3, 12))
            for action in ['api', 'subscription'] if feed else ['api']:
                started = time.monotonic()
                result = {'panel_id': panel.pk, 'mode': mode, 'action': action}
                try:
                    payload = client.get_system() if action == 'api' else client.fetch_native_links(source)
                    result['ok'] = True
                    if action == 'subscription': result['configs'] = len(payload)
                except Exception as exc:
                    result.update(ok=False, error=error(exc))
                result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
                print(json.dumps(result), flush=True)
PY
