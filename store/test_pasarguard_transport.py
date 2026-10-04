"""Real local HTTPS handshakes: compatibility must retain certificate validation."""
import json
import ssl
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from django.test import SimpleTestCase
from urllib3.connection import HTTPSConnection

from .panels.pasarguard.client import PasarGuardClient
from .panels.pasarguard.errors import PasarGuardIntegrationError


class PasarGuardTransportTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tempdir.cleanup)
        cert = Path(cls.tempdir.name) / "cert.pem"
        key = Path(cls.tempdir.name) / "key.pem"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
             "-keyout", str(key), "-out", str(cert)],
            check=True, capture_output=True,
        )
        cls.cert = str(cert)

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                body = json.dumps({"tls": self.connection.version(), "peer_port": self.client_address[1]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                body = json.dumps({"tls": self.connection.version(), "payload": payload}).encode()
                self.send_response(201 if self.command == "POST" else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_PUT = do_POST

            def log_message(self, *_args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(cert), str(key))
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        cls.url = f"https://127.0.0.1:{cls.server.server_port}"

    def make_client(self):
        client = PasarGuardClient(SimpleNamespace(url=self.url, password="test-only"), timeout=2)
        client.session.trust_env = False
        self.addCleanup(client.session.close)
        return client

    def test_api_and_subscription_negotiate_tls12_with_validated_certificate(self):
        client = self.make_client()
        client.session.verify = self.cert
        self.assertEqual(client.get_system()["tls"], "TLSv1.2")
        response = client._subscription_get(f"{self.url}/subscription/links")
        self.assertEqual(response.json()["tls"], "TLSv1.2")

    def test_untrusted_certificate_is_rejected(self):
        with self.assertRaises(PasarGuardIntegrationError) as caught:
            self.make_client().get_system()
        self.assertEqual(caught.exception.error_code, "pasarguard_network_error")
        self.assertIsInstance(caught.exception.__cause__, requests.exceptions.SSLError)

    def test_repeated_api_reads_use_separate_verified_connections(self):
        client = self.make_client()
        client.session.verify = self.cert
        first = client.get_system()
        second = client.get_system()
        self.assertEqual(first["tls"], "TLSv1.2")
        self.assertEqual(second["tls"], "TLSv1.2")
        self.assertNotEqual(first["peer_port"], second["peer_port"])

    def test_json_create_uses_one_tls_write_and_preserves_body(self):
        client = self.make_client()
        client.session.verify = self.cert
        sends = []
        original_send = HTTPSConnection.send

        def capture_send(connection, data):
            sends.append(data)
            return original_send(connection, data)

        payload = {"username": "local-test", "group_ids": [29], "note": "تست"}
        for method in ("POST", "PUT"):
            with self.subTest(method=method):
                sends.clear()
                with patch.object(HTTPSConnection, "send", capture_send):
                    result = client.request(method, "/api/user", json=payload, expected_statuses=(200, 201))
                self.assertEqual(result["tls"], "TLSv1.2")
                self.assertEqual(result["payload"], payload)
                self.assertEqual(len(sends), 1)
                headers, body = sends[0].split(b"\r\n\r\n", 1)
                self.assertIn(f"{method} /api/user HTTP/1.1".encode(), headers)
                self.assertIn(f"Content-Length: {len(body)}".encode(), headers)
                self.assertEqual(json.loads(body), payload)

    def test_large_json_body_is_not_buffered_and_other_sessions_are_unchanged(self):
        client = self.make_client()
        client.session.verify = self.cert
        sends = []
        original_send = HTTPSConnection.send

        def capture_send(connection, data):
            sends.append(data)
            return original_send(connection, data)

        payload = {"note": "x" * 70000}
        with patch.object(HTTPSConnection, "send", capture_send):
            result = client.create_user(payload)
        self.assertEqual(result["payload"], payload)
        self.assertEqual(len(sends), 2)
        with requests.Session() as other_session:
            other_pool = other_session.adapters["https://"].poolmanager.pool_classes_by_scheme["https"]
            self.assertIs(other_pool.ConnectionCls, HTTPSConnection)
