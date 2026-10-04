"""Real local HTTPS handshakes: compatibility must retain certificate validation."""
import json
import ssl
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import requests
from django.test import SimpleTestCase

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
