"""Exercise the real gateway against loopback fixtures, without an LLM or API keys."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import httpx
import pytest

CADDYFILE = Path(__file__).resolve().parents[1] / "Caddyfile"
UNAVAILABLE = {
    "error": {
        "code": "llm_service_unavailable",
        "message": "LLM service is unavailable.",
    }
}
AUTHORIZATION = "Bearer local-fixture-token"


class FixtureBackend(ThreadingHTTPServer):
    daemon_threads = True
    authorizations: list[str | None]
    stream_release: threading.Event

    def __init__(self, port: int) -> None:
        super().__init__(("127.0.0.1", port), FixtureHandler)
        self.authorizations = []
        self.stream_release = threading.Event()


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def reply(self, status: int, body: dict[str, object]) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def authorized(self) -> bool:
        backend = cast(FixtureBackend, self.server)
        authorization = self.headers.get("Authorization")
        backend.authorizations.append(authorization)
        if authorization != AUTHORIZATION:
            self.reply(401, {"error": {"code": "invalid_api_key"}})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self.reply(200, {"status": "ok"})
        elif self.path == "/":
            self.reply(200, {"ui": "fixture"})
        elif self.authorized():
            if self.path == "/forced-unavailable":
                self.reply(503, {"error": "raw backend loading response"})
            elif self.path == "/props":
                self.reply(200, {"total_slots": 6})
            else:
                self.reply(200, {"data": [{"id": "fixture-model"}]})

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if not self.authorized():
            return
        backend = cast(FixtureBackend, self.server)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n')
        self.wfile.flush()
        if backend.stream_release.wait(10):
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()


def unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def fixture_certificates(directory: Path) -> tuple[Path, Path, Path]:
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("OpenSSL is required to generate temporary gateway certificates")
    ca, ca_key = directory / "ca.pem", directory / "ca.key"
    cert, key = directory / "server.pem", directory / "server.key"
    request, extensions = directory / "server.csr", directory / "server.ext"
    extensions.write_text(
        "subjectAltName=IP:127.0.0.1\n"
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "subjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid,issuer\n"
        "extendedKeyUsage=serverAuth\n",
        encoding="ascii",
    )
    commands = [
        [
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=Local Gateway Test CA",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca),
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-addext",
            "subjectKeyIdentifier=hash",
        ],
        [
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(request),
        ],
        [
            "x509",
            "-req",
            "-in",
            str(request),
            "-CA",
            str(ca),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-days",
            "1",
            "-extfile",
            str(extensions),
            "-out",
            str(cert),
        ],
    ]
    for command in commands:
        subprocess.run([openssl, *command], capture_output=True, check=True, timeout=30)
    return ca, cert, key


def fixture_config(cert: Path, key: Path, tls_port: int, upstream_port: int) -> str:
    config = CADDYFILE.read_text(encoding="utf-8")
    assert re.search(r"\bauto_https\s+off\b", config)
    config, count = re.subn(
        r"(?m)^https://satisshin\.top:4000\s*\{",
        f"https://127.0.0.1:{tls_port} {{\n\tbind 127.0.0.1",
        config,
        count=1,
    )
    assert count == 1
    for production, fixture in [
        ("/etc/satisshin/tls/current/fullchain.pem", cert),
        ("/etc/satisshin/tls/current/privkey.pem", key),
    ]:
        assert production in config
        config = config.replace(production, json.dumps(fixture.as_posix()))
    assert "127.0.0.1:4001" in config
    config = config.replace("127.0.0.1:4001", f"127.0.0.1:{upstream_port}")
    config, count = re.subn(r"(?m)^\{[ \t]*$", "{\n\tadmin off", config, count=1)
    assert count == 1
    assert "/etc/satisshin/" not in config
    return config


def wait_status(
    client: httpx.Client,
    path: str,
    status: int,
    process: subprocess.Popen[bytes],
    log: Path,
) -> httpx.Response:
    deadline = time.monotonic() + 15
    last = "no response"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("Caddy exited: " + log.read_text(errors="replace"))
        try:
            response = client.get(path, headers={"Authorization": AUTHORIZATION})
            if response.status_code == status:
                return response
            last = f"HTTP {response.status_code}: {response.text}"
        except httpx.RequestError as error:
            last = str(error)
        time.sleep(0.1)
    pytest.fail(f"Expected HTTP {status}; {last}\n{log.read_text(errors='replace')}")


@pytest.mark.skipif(
    not os.environ.get("CADDY_BIN"), reason="Set CADDY_BIN to run Caddy"
)
def test_gateway_reports_outages_and_preserves_authenticated_streams(
    tmp_path: Path,
) -> None:
    caddy = shutil.which(os.environ["CADDY_BIN"])
    assert caddy, "CADDY_BIN must point to an existing Caddy executable"
    ca, cert, key = fixture_certificates(tmp_path)
    tls_port, upstream_port = unused_port(), unused_port()
    while upstream_port == tls_port:
        upstream_port = unused_port()
    config = tmp_path / "Caddyfile"
    config.write_text(
        fixture_config(cert, key, tls_port, upstream_port), encoding="utf-8"
    )
    log = tmp_path / "caddy.log"
    environment = dict(os.environ)
    for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA"):
        environment[variable] = str(tmp_path / variable.lower())
    backend: FixtureBackend | None = None
    thread: threading.Thread | None = None
    with log.open("wb") as output:
        process = subprocess.Popen(
            [caddy, "run", "--config", str(config), "--adapter", "caddyfile"],
            stdout=output,
            stderr=subprocess.STDOUT,
            env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            context = ssl.create_default_context(cafile=str(ca))
            url = f"https://127.0.0.1:{tls_port}"
            with httpx.Client(
                base_url=url, verify=context, trust_env=False, timeout=3
            ) as client:
                unavailable = wait_status(client, "/v1/models", 503, process, log)
                assert unavailable.json() == UNAVAILABLE
                assert unavailable.headers["Content-Type"].startswith(
                    "application/json"
                )
                assert client.get("/slots").status_code == 404
                assert client.post("/props").status_code == 404
                with (
                    httpx.Client(
                        verify=ssl.create_default_context(), trust_env=False, timeout=3
                    ) as untrusted,
                    pytest.raises(httpx.ConnectError),
                ):
                    untrusted.get(url + "/health")

                backend = FixtureBackend(upstream_port)
                thread = threading.Thread(target=backend.serve_forever, daemon=True)
                thread.start()
                healthy = wait_status(client, "/health", 200, process, log)
                assert healthy.json() == {"status": "ok"}
                models = client.get(
                    "/v1/models", headers={"Authorization": AUTHORIZATION}
                )
                assert models.status_code == 200
                assert models.json() == {"data": [{"id": "fixture-model"}]}
                assert AUTHORIZATION in backend.authorizations
                unauthorized = client.get("/v1/models")
                assert unauthorized.status_code == 401
                assert unauthorized.json() == {"error": {"code": "invalid_api_key"}}
                invalid_key = client.get(
                    "/v1/models",
                    headers={"Authorization": "Bearer invalid-fixture-token"},
                )
                assert invalid_key.status_code == 401
                assert client.get("/").json() == {"ui": "fixture"}
                properties = client.get(
                    "/props", headers={"Authorization": AUTHORIZATION}
                )
                assert properties.status_code == 200
                assert properties.json() == {"total_slots": 6}
                assert client.get("/props").status_code == 401

                forwarded = len(backend.authorizations)
                for method, path in [
                    ("GET", "/slots"),
                    ("GET", "/slots?fail_on_no_slot=1"),
                    ("GET", "/slots/0"),
                    ("GET", "/slots%2f0"),
                    ("POST", "/slots/0?action=erase"),
                    ("GET", "/s%6cots"),
                    ("GET", "/metrics"),
                    ("GET", "/lora-adapters"),
                    ("POST", "/lora-adapters"),
                    ("GET", "/lora-adapters/0"),
                    *(
                        (method, path)
                        for method in ("POST", "PUT", "PATCH", "DELETE")
                        for path in ("/props", "/props/example")
                    ),
                ]:
                    blocked = client.request(
                        method, path, headers={"Authorization": AUTHORIZATION}
                    )
                    assert blocked.status_code == 404, (method, path)
                assert len(backend.authorizations) == forwarded
                assert client.get("/slots").status_code == 404

                with client.stream(
                    "POST",
                    "/v1/chat/completions",
                    headers={"Authorization": AUTHORIZATION},
                    json={"stream": True},
                ) as response:
                    assert response.status_code == 200
                    assert response.headers["Content-Type"].startswith(
                        "text/event-stream"
                    )
                    lines = response.iter_lines()
                    assert (
                        next(lines)
                        == 'data: {"choices":[{"delta":{"content":"first"}}]}'
                    )
                    backend.stream_release.set()
                    assert "data: [DONE]" in list(lines)

                loading = client.get(
                    "/forced-unavailable", headers={"Authorization": AUTHORIZATION}
                )
                assert loading.status_code == 503
                assert loading.json() == UNAVAILABLE
                backend.shutdown()
                backend.server_close()
                thread.join(timeout=3)
                backend = None
                assert (
                    wait_status(client, "/health", 503, process, log).json()
                    == UNAVAILABLE
                )
        finally:
            if backend is not None:
                backend.stream_release.set()
                backend.shutdown()
                backend.server_close()
            if thread is not None:
                thread.join(timeout=3)
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
