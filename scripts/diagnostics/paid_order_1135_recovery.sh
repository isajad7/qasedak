#!/usr/bin/env bash
set -Eeuo pipefail
source /etc/qasedak/dev-deploy.conf
[[ "$(cat "$QASEDAK_DEV_ROOT/.qasedak-development-target")" == 'isajad7/qasedak:development' ]]
app="$(docker ps --filter "label=com.docker.compose.project=$QASEDAK_DEV_PROJECT" --filter "label=com.docker.compose.service=$QASEDAK_DEV_SERVICE" --format '{{.ID}}')"
[[ "$app" =~ ^[0-9a-f]{12,64}$ ]]
[[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$app")" == "$QASEDAK_DEV_SERVICE" ]]
[[ "$(docker inspect -f '{{.Config.Image}}' "$app")" == 'qasedak-development:f2e51b8cc3e77c0169b9d1339f8af75bf01351de' ]]
docker exec -i "$app" python manage.py shell --no-imports <<'PY'
import json,time
from urllib.parse import urlsplit
import requests
from store.models import Order
from store.provisioning_services import approve_and_provision_order
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool

class CombinedHTTPSConnection(HTTPSConnection):
    def request(self, method, url, body=None, headers=None, **kwargs):
        combine = (method.upper() in {"POST", "PUT", "PATCH"} and isinstance(body, (bytes, bytearray)) and len(body) <= 65536 and not kwargs.get("chunked", False))
        if not combine:
            return super().request(method, url, body=body, headers=headers, **kwargs)
        self._request_parts = []
        try:
            super().request(method, url, body=body, headers=headers, **kwargs)
            message = b"".join(self._request_parts)
        finally:
            del self._request_parts
        self.send(message)

    def send(self, data):
        parts = getattr(self, "_request_parts", None)
        if parts is not None:
            parts.append(data)
        else:
            super().send(data)

class CombinedHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = CombinedHTTPSConnection

from store.panels.pasarguard.client import PasarGuardHTTPSAdapter
original_init=PasarGuardHTTPSAdapter.init_poolmanager
def combined_init(self,*args,**kwargs):
    original_init(self,*args,**kwargs)
    self.poolmanager.pool_classes_by_scheme={**self.poolmanager.pool_classes_by_scheme,'https':CombinedHTTPSConnectionPool}
PasarGuardHTTPSAdapter.init_poolmanager=combined_init
original=requests.Session.request
def traced(self,method,url,**kwargs):
    path=urlsplit(str(url)).path
    if path.startswith('/api/user/'): path='/api/user/[order-user]'
    elif not path.startswith('/api/'): path='[subscription]'
    started=time.monotonic()
    try:
        response=original(self,method,url,**kwargs)
        print(json.dumps({'request_method':method,'request_route':path,'status':response.status_code,'seconds':round(time.monotonic()-started,2)}),flush=True)
        return response
    except requests.RequestException as exc:
        print(json.dumps({'request_method':method,'request_route':path,'error_type':type(exc).__name__,'seconds':round(time.monotonic()-started,2)}),flush=True)
        raise
requests.Session.request=traced
order=Order.objects.select_related('plan','store').get(pk=1136,order_tracking_code='0d0fbe5401a549e2a76a735a4388def1',store_id=1)
if not order.is_paid or order.verification_status!='verified': raise RuntimeError('Order is not a verified paid purchase')
if order.status==Order.Status.COMPLETED:
    print(json.dumps({'order_id':order.pk,'already_completed':True}),flush=True)
else:
    result=approve_and_provision_order(order,source='authorized_incident_recovery',notify=True)
    order.refresh_from_db()
    print(json.dumps({'order_id':order.pk,'ok':result.ok,'status':order.status,'provisioning_status':order.provisioning_status,'config_count':(order.metadata or {}).get('plan_delivery_v2',{}).get('config_link_count')}),flush=True)
PY
