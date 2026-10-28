"""Offline DNS updater tests: no live IP discovery or Porkbun requests."""

from __future__ import annotations

import contextlib
import http.client
import importlib.util
import io
import json
import os
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import TracebackType
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "sync_dns.py"
SPEC = importlib.util.spec_from_file_location("sync_dns", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)

OLD_IP = "9.9.9.9"
NEW_IP = "27.235.241.38"


def record(ip: str = OLD_IP, **fields: str) -> dict[str, str]:
    return {
        "id": "123",
        "name": "satisshin.top",
        "type": "A",
        "content": ip,
        "ttl": "600",
        **fields,
    }


def response(*records: dict[str, str]) -> dict[str, object]:
    return {"status": "SUCCESS", "records": list(records)}


class FakeResponse:
    status = 200

    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.body = body
        self.headers = headers or {}
        self.read_limit: int | None = None

    def read(self, limit: int) -> bytes:
        self.read_limit = limit
        return self.body[:limit]

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class IpTests(unittest.TestCase):
    def test_accepts_only_global_ipv4(self) -> None:
        self.assertEqual(sync.public_ipv4(f"{NEW_IP}\n"), NEW_IP)
        for invalid in [
            "10.0.0.1",
            "172.16.0.1",
            "192.168.0.1",
            "100.64.0.1",
            "127.0.0.1",
            "169.254.1.1",
            "0.0.0.0",
            "192.0.0.8",
            "192.0.2.1",
            "192.88.99.1",
            "198.18.0.1",
            "198.51.100.1",
            "203.0.113.1",
            "224.0.0.1",
            "240.0.0.1",
            "255.255.255.255",
            "::1",
            "2606:4700::1111",
            "example.com",
        ]:
            with self.subTest(ip=invalid), self.assertRaises(sync.SyncError):
                sync.public_ipv4(invalid)

    def test_discovery_requires_agreement_from_both_official_sources(self) -> None:
        with mock.patch.object(
            sync, "request_bytes", side_effect=[NEW_IP.encode(), f"{NEW_IP}\n".encode()]
        ) as request:
            self.assertEqual(sync.discover_ipv4(), NEW_IP)
        self.assertEqual(
            request.call_args_list,
            [
                mock.call(source, limit=sync.IP_RESPONSE_BYTES)
                for source in sync.IP_SOURCES
            ],
        )

    def test_discovery_disagreement_or_invalid_source_prevents_dns_access(self) -> None:
        for replies in [
            [NEW_IP.encode(), OLD_IP.encode()],
            [NEW_IP.encode(), b"10.0.0.1"],
            [b"not-ip"],
        ]:
            with (
                self.subTest(replies=replies),
                mock.patch.object(sync, "request_bytes", side_effect=replies),
                mock.patch.object(sync, "api_request") as api,
                self.assertRaises(sync.SyncError),
            ):
                sync.sync_dns("satisshin.top", apply=True)
            api.assert_not_called()


class DnsTests(unittest.TestCase):
    def test_same_ip_performs_no_write_even_when_apply_is_requested(self) -> None:
        with mock.patch.object(
            sync, "api_request", return_value=response(record(NEW_IP))
        ) as api:
            self.assertIn(
                "already current",
                sync.sync_dns("satisshin.top", ipv4=NEW_IP, apply=True),
            )
        api.assert_called_once_with("dns/retrieveByNameType/satisshin.top/A")

    def test_default_dry_run_queries_without_writing_or_discovering_explicit_ip(
        self,
    ) -> None:
        with (
            mock.patch.object(
                sync, "api_request", return_value=response(record())
            ) as api,
            mock.patch.object(sync, "discover_ipv4") as discovery,
        ):
            self.assertIn("Dry-run", sync.sync_dns("satisshin.top", ipv4=NEW_IP))
        api.assert_called_once_with("dns/retrieveByNameType/satisshin.top/A")
        discovery.assert_not_called()

    def test_missing_multiple_or_unexpected_records_never_write(self) -> None:
        replies = [
            response(),
            response(record(), record(id="124")),
            response(record(name="www.satisshin.top")),
            response(record(type="AAAA")),
            response(record(id="123/other")),
            {"status": "SUCCESS", "records": "bad"},
        ]
        for reply in replies:
            with (
                self.subTest(reply=reply),
                mock.patch.object(sync, "api_request", return_value=reply) as api,
                self.assertRaises(sync.SyncError),
            ):
                sync.sync_dns("satisshin.top", ipv4=NEW_IP, apply=True)
            self.assertEqual(api.call_count, 1)

    def test_apply_updates_only_selected_id_content_and_ttl_then_verifies(self) -> None:
        with mock.patch.object(
            sync,
            "api_request",
            side_effect=[
                response(record()),
                {"status": "SUCCESS"},
                response(record(NEW_IP)),
            ],
        ) as api:
            self.assertIn(
                "updated and verified",
                sync.sync_dns("satisshin.top", ipv4=NEW_IP, apply=True),
            )
        self.assertEqual(
            api.call_args_list,
            [
                mock.call("dns/retrieveByNameType/satisshin.top/A"),
                mock.call(
                    "dns/edit/satisshin.top/123", {"content": NEW_IP, "ttl": 600}
                ),
                mock.call("dns/retrieveByNameType/satisshin.top/A"),
            ],
        )

    def test_post_write_verification_rejects_changed_id_ip_or_ttl(self) -> None:
        for final_record in [
            record(NEW_IP, id="124"),
            record(OLD_IP),
            record(NEW_IP, ttl="300"),
        ]:
            with (
                self.subTest(final=final_record),
                mock.patch.object(
                    sync,
                    "api_request",
                    side_effect=[
                        response(record()),
                        {"status": "SUCCESS"},
                        response(final_record),
                    ],
                ) as api,
                self.assertRaisesRegex(sync.SyncError, "could not be verified"),
            ):
                sync.sync_dns("satisshin.top", ipv4=NEW_IP, apply=True)
            self.assertEqual(api.call_count, 3)

    def test_api_dns_warning_is_not_reported_as_success(self) -> None:
        with (
            mock.patch.object(
                sync,
                "api_request",
                side_effect=[
                    response(record()),
                    {"status": "SUCCESS", "warnings": ["synthetic warning"]},
                    response(record(NEW_IP)),
                ],
            ),
            self.assertRaisesRegex(sync.SyncError, "warning"),
        ):
            sync.sync_dns("satisshin.top", ipv4=NEW_IP, apply=True)

    def test_cli_defaults_to_dry_run(self) -> None:
        with (
            mock.patch.object(sync.sys, "platform", "linux"),
            mock.patch.object(sync, "sync_dns", return_value="Dry-run") as update,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(sync.main(["--ipv4", NEW_IP]), 0)
        update.assert_called_once_with("satisshin.top", ipv4=NEW_IP, apply=False)


class TransportTests(unittest.TestCase):
    def setUp(self) -> None:
        patch = mock.patch.dict(
            os.environ,
            {
                "PORKBUN_API_KEY": "synthetic-api-key",
                "PORKBUN_SECRET_API_KEY": "synthetic-secret-key",
            },
        )
        patch.start()
        self.addCleanup(patch.stop)

    def test_api_uses_fixed_https_post_and_bounded_read(self) -> None:
        reply = FakeResponse(json.dumps(response(record())).encode())
        opener = mock.Mock()
        opener.open.return_value = reply
        with mock.patch.object(
            sync.urllib.request, "build_opener", return_value=opener
        ) as build:
            self.assertEqual(
                sync.api_request("dns/retrieveByNameType/satisshin.top/A"),
                response(record()),
            )
        request = opener.open.call_args.args[0]
        self.assertEqual(
            request.full_url, f"{sync.API_BASE}/dns/retrieveByNameType/satisshin.top/A"
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            json.loads(request.data),
            {"apikey": "synthetic-api-key", "secretapikey": "synthetic-secret-key"},
        )
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": sync.HTTP_TIMEOUT})
        self.assertEqual(reply.read_limit, sync.MAX_RESPONSE_BYTES + 1)
        self.assertIsInstance(build.call_args.args[0], urllib.request.ProxyHandler)
        self.assertEqual(build.call_args.args[0].proxies, {})

    def test_http_failure_and_api_error_never_disclose_credentials_or_body(
        self,
    ) -> None:
        cases = [
            urllib.error.HTTPError(
                "https://api.porkbun.com/",
                403,
                "synthetic-secret-key",
                {},
                io.BytesIO(b"synthetic-response-secret"),
            ),
            FakeResponse(b'{"status":"ERROR","message":"synthetic-response-secret"}'),
            FakeResponse(b"synthetic-response-secret not-json"),
        ]
        for case in cases:
            opener = mock.Mock()
            if isinstance(case, Exception):
                opener.open.side_effect = case
            else:
                opener.open.return_value = case
            error = io.StringIO()
            with (
                mock.patch.object(sync.sys, "platform", "linux"),
                mock.patch.object(
                    sync.urllib.request, "build_opener", return_value=opener
                ),
                contextlib.redirect_stderr(error),
            ):
                self.assertEqual(sync.main(["--ipv4", NEW_IP, "--apply"]), 1)
            for secret in [
                "synthetic-api-key",
                "synthetic-secret-key",
                "synthetic-response-secret",
            ]:
                self.assertNotIn(secret, error.getvalue())

    def test_response_size_limits_are_enforced(self) -> None:
        for reply in [
            FakeResponse(b"x" * 6),
            FakeResponse(b"", {"Content-Length": "6"}),
        ]:
            opener = mock.Mock()
            opener.open.return_value = reply
            with (
                mock.patch.object(
                    sync.urllib.request, "build_opener", return_value=opener
                ),
                self.assertRaises(sync.SyncError),
            ):
                sync.request_bytes("https://api.ipify.org", limit=5)

    def test_redirects_are_never_followed(self) -> None:
        self.assertIsNone(
            sync.NoRedirect().redirect_request(
                urllib.request.Request("https://api.porkbun.com/"),
                mock.Mock(spec=http.client.HTTPResponse),
                302,
                "",
                http.client.HTTPMessage(),
                "https://elsewhere.example/",
            )
        )

    def test_help_never_discloses_environment_values(self) -> None:
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            self.assertRaises(SystemExit) as raised,
        ):
            sync.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertNotIn("synthetic-secret-key", output.getvalue())


if __name__ == "__main__":
    unittest.main()
