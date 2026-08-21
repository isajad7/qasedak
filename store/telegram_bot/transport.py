import base64
import json
import socket
import ssl
import uuid
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from django.conf import settings

from store.bot_proxy import telegram_proxy_url
from store.models import BotConfiguration


TELEGRAM_API_HOST = "api.telegram.org"


@dataclass
class TelegramHTTPResponse:
    status_code: int
    reason: str
    headers: dict
    content: bytes

    @property
    def ok(self):
        return 200 <= int(self.status_code or 0) < 400

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.content.decode("utf-8"))

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"Telegram API HTTP {self.status_code} {self.reason}".strip())


def telegram_api_override_ip():
    return str(getattr(settings, "TELEGRAM_API_IP", "") or "").strip()


def telegram_api_request(config, request_method, url, *, json_payload=None, data=None, files=None, timeout=None):
    if config.provider != BotConfiguration.Provider.TELEGRAM:
        return None
    override_ip = telegram_api_override_ip()
    proxy_url = telegram_proxy_url()
    if not override_ip or not proxy_url:
        return None

    parsed_url = urlsplit(url)
    if parsed_url.scheme != "https" or parsed_url.hostname != TELEGRAM_API_HOST:
        return None

    body = b""
    headers = {
        "Host": TELEGRAM_API_HOST,
        "User-Agent": "qasedak-telegram-transport/1.0",
        "Accept": "application/json",
        "Connection": "close",
    }
    if json_payload is not None:
        body = json.dumps(json_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif files:
        body, content_type = _encode_multipart(data or {}, files)
        headers["Content-Type"] = content_type
    elif data:
        body = _encode_form(data)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if body:
        headers["Content-Length"] = str(len(body))

    path = parsed_url.path or "/"
    if parsed_url.query:
        path = f"{path}?{parsed_url.query}"
    connect_timeout, read_timeout = _timeout_pair(timeout)

    proxy = urlsplit(proxy_url)
    if proxy.scheme and proxy.scheme.lower() != "http":
        raise RuntimeError("TELEGRAM_API_IP override currently requires an HTTP proxy.")
    if not proxy.hostname or not proxy.port:
        raise RuntimeError("Telegram proxy URL must include host and port.")

    with socket.create_connection((proxy.hostname, proxy.port), timeout=connect_timeout) as raw_sock:
        raw_sock.settimeout(connect_timeout)
        _open_proxy_tunnel(raw_sock, proxy, override_ip, 443)
        raw_sock.settimeout(read_timeout)
        context = ssl.create_default_context()
        with context.wrap_socket(raw_sock, server_hostname=TELEGRAM_API_HOST) as tls_sock:
            tls_sock.settimeout(read_timeout)
            _send_http_request(tls_sock, request_method, path, headers, body)
            return _read_http_response(tls_sock)


def _timeout_pair(timeout):
    default_connect = float(getattr(settings, "BOT_API_CONNECT_TIMEOUT_SECONDS", 3))
    default_read = float(getattr(settings, "BOT_API_READ_TIMEOUT_SECONDS", 8))
    if timeout is None:
        return default_connect, default_read
    if isinstance(timeout, (tuple, list)):
        connect = float(timeout[0]) if len(timeout) > 0 and timeout[0] is not None else default_connect
        read = float(timeout[1]) if len(timeout) > 1 and timeout[1] is not None else default_read
        return connect, read
    value = float(timeout)
    return value, value


def _open_proxy_tunnel(sock, proxy, target_host, target_port):
    lines = [
        f"CONNECT {target_host}:{target_port} HTTP/1.1",
        f"Host: {target_host}:{target_port}",
        "Proxy-Connection: Keep-Alive",
    ]
    if proxy.username:
        username = unquote(proxy.username)
        password = unquote(proxy.password or "")
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        lines.append(f"Proxy-Authorization: Basic {token}")
    request = "\r\n".join(lines) + "\r\n\r\n"
    sock.sendall(request.encode("ascii"))
    response_head = _read_until(sock, b"\r\n\r\n", limit=16 * 1024)
    status_line = response_head.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
    parts = status_line.split(" ", 2)
    status_code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    if status_code != 200:
        raise RuntimeError(f"Telegram proxy CONNECT failed: {status_line}")


def _send_http_request(sock, method, path, headers, body):
    header_lines = [f"{str(method or 'GET').upper()} {path} HTTP/1.1"]
    header_lines.extend(f"{key}: {value}" for key, value in headers.items())
    request = ("\r\n".join(header_lines) + "\r\n\r\n").encode("utf-8") + body
    sock.sendall(request)


def _read_http_response(sock):
    first = _read_until(sock, b"\r\n\r\n", limit=64 * 1024)
    head, _, body = first.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1", errors="replace").split("\r\n")
    status_line = lines[0] if lines else ""
    parts = status_line.split(" ", 2)
    status_code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    reason = parts[2] if len(parts) > 2 else ""
    headers = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()

    transfer_encoding = headers.get("transfer-encoding", "").lower()
    if "chunked" in transfer_encoding:
        body = _read_chunked_body(sock, body)
    elif headers.get("content-length"):
        expected = int(headers["content-length"])
        body = body + _read_exact(sock, max(expected - len(body), 0))
        body = body[:expected]
    else:
        chunks = [body]
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        body = b"".join(chunks)
    return TelegramHTTPResponse(status_code=status_code, reason=reason, headers=headers, content=body)


def _read_until(sock, delimiter, *, limit):
    data = b""
    while delimiter not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            raise RuntimeError("Telegram proxy response header is too large.")
    return data


def _read_exact(sock, size):
    chunks = []
    remaining = int(size or 0)
    while remaining > 0:
        chunk = sock.recv(min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_chunked_body(sock, buffered):
    data = buffered
    chunks = []
    while True:
        while b"\r\n" not in data:
            data += sock.recv(4096)
        line, _, data = data.partition(b"\r\n")
        size_text = line.split(b";", 1)[0].strip()
        size = int(size_text or b"0", 16)
        if size == 0:
            break
        while len(data) < size + 2:
            data += sock.recv(max(4096, size + 2 - len(data)))
        chunks.append(data[:size])
        data = data[size + 2 :]
    return b"".join(chunks)


def _encode_form(data):
    from urllib.parse import urlencode

    return urlencode({key: value for key, value in data.items() if value is not None}).encode("utf-8")


def _encode_multipart(data, files):
    boundary = f"----qasedak-telegram-{uuid.uuid4().hex}"
    parts = []
    for key, value in (data or {}).items():
        if value is None:
            continue
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
        parts.append(str(value).encode("utf-8"))
        parts.append(b"\r\n")
    for key, file_value in (files or {}).items():
        filename, file_obj = file_value
        content = file_obj.read() if hasattr(file_obj, "read") else bytes(file_obj or b"")
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(
            (
                f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8")
        )
        parts.append(content)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
