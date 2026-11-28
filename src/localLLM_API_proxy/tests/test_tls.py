"""Exercise TLS on loopback without contacting the real server or running a model."""

from __future__ import annotations

import shutil
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import certifi
import httpx
import pytest

import llama_wrapper as wrapper


@pytest.fixture(scope="module")
def tls_certificates(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[bool, tuple[Path, Path]]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("OpenSSL is needed to generate temporary TLS test certificates")
    directory = tmp_path_factory.mktemp("tls")
    certificates = {}
    for matching_name in (True, False):
        cert = directory / f"{matching_name}.pem"
        key = directory / f"{matching_name}.key"
        alternative_name = "IP:127.0.0.1" if matching_name else "DNS:wrong.invalid"
        subprocess.run(
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                "/CN=loopback-test",
                "-addext",
                f"subjectAltName={alternative_name}",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
        certificates[matching_name] = cert, key
    return certificates


@contextmanager
def loopback_tls_server(cert: Path, key: Path) -> Iterator[tuple[str, list[str]]]:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - HTTP server hook name
            requests.append(self.headers.get("Authorization", ""))
            body = b'{"status":"ok"}'
            self.send_response(200 if self.path == "/health" else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.mark.parametrize(
    ("trust_certificate", "matching_name", "error"),
    [
        (False, True, "self-signed certificate"),
        (True, False, "IP address mismatch"),
        (True, True, None),
    ],
)
def test_default_tls_checks_chain_and_hostname(
    monkeypatch: pytest.MonkeyPatch,
    tls_certificates: dict[bool, tuple[Path, Path]],
    trust_certificate: bool,
    matching_name: bool,
    error: str | None,
) -> None:
    cert, key = tls_certificates[matching_name]
    if trust_certificate:
        # Replace only the test trust store; the client still uses verify=True.
        monkeypatch.setattr(certifi, "where", lambda: str(cert))
    with loopback_tls_server(cert, key) as (url, requests):
        with wrapper.Llama(
            backend="remote", server_url=url, server_token="fake-test-key", timeout=2
        ) as client:
            if error is None:
                assert client.health() == {"status": "ok"}
                assert requests == ["Bearer fake-test-key"]
            else:
                with pytest.raises(httpx.ConnectError, match=error):
                    client.health()
                assert requests == []


@contextmanager
def stalled_tls_server(
    cert: Path, key: Path, *, trickle_body: bool
) -> Iterator[tuple[str, threading.Event, threading.Event]]:
    started = threading.Event()
    disconnected = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - HTTP server hook name
            if self.path == "/health":
                body = b'{"status":"ok"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.connection.settimeout(2)
            started.set()
            try:
                if trickle_body:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.01)
                elif self.connection.recv(1) == b"":
                    disconnected.set()
            except (ConnectionError, ssl.SSLError):
                disconnected.set()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}", started, disconnected
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.mark.parametrize("action", ["cancel", "timeout", "close"])
@pytest.mark.parametrize("trickle_body", [False, True])
def test_real_tls_request_cancellation_closes_only_its_socket(
    monkeypatch: pytest.MonkeyPatch,
    tls_certificates: dict[bool, tuple[Path, Path]],
    action: str,
    trickle_body: bool,
) -> None:
    cert, key = tls_certificates[True]
    monkeypatch.setattr(certifi, "where", lambda: str(cert))
    with stalled_tls_server(cert, key, trickle_body=trickle_body) as (
        url,
        started,
        disconnected,
    ):
        with wrapper.Llama(
            backend="remote", server_url=url, server_token="fake-test-key", timeout=5
        ) as client:
            request = wrapper._RemoteRequest(0.2 if action == "timeout" else 5)

            def receive() -> None:
                with client._request_context(request):
                    client._request("GET", "/slow")

            with ThreadPoolExecutor(max_workers=2) as pool:
                result = pool.submit(receive)
                assert started.wait(1)
                # 같은 공유 클라이언트로 다른 요청은 정상 처리된다.
                assert client.health() == {"status": "ok"}
                if action == "cancel":
                    request.cancel()
                elif action == "close":
                    pool.submit(client.close).result(timeout=1)
                with pytest.raises((TimeoutError, httpx.ReadTimeout)):
                    result.result(timeout=1)
                assert disconnected.wait(1)
                if action != "close":
                    assert client.health() == {"status": "ok"}
