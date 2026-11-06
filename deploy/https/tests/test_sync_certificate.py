"""Offline tests: no Porkbun API or real server is contacted."""

from __future__ import annotations

import contextlib
import http.client
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from types import TracebackType
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "sync_certificate.py"
SPEC = importlib.util.spec_from_file_location("sync_certificate", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class FakeResponse:
    status = 200

    def __init__(self, data: bytes, headers: dict[str, str] | None = None) -> None:
        self.data = data
        self.headers = headers or {}
        self.read_limit = None

    def read(self, limit: int) -> bytes:
        self.read_limit = limit
        return self.data[:limit]

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = mock.patch.dict(
            os.environ,
            {
                "PORKBUN_API_KEY": "test-api-secret",
                "PORKBUN_SECRET_API_KEY": "test-private-secret",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_retrieves_existing_bundle_with_bounded_request(self) -> None:
        response = FakeResponse(
            json.dumps(
                {"status": "SUCCESS", "certificatechain": "chain", "privatekey": "key"}
            ).encode()
        )
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(
            sync.urllib.request, "build_opener", return_value=opener
        ) as build:
            self.assertEqual(sync.retrieve_bundle("satisshin.top"), (b"chain", b"key"))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            request.full_url, f"{sync.API_BASE}/ssl/retrieve/satisshin.top"
        )
        self.assertEqual(
            json.loads(request.data),
            {"apikey": "test-api-secret", "secretapikey": "test-private-secret"},
        )
        self.assertEqual(opener.open.call_args.kwargs["timeout"], sync.HTTP_TIMEOUT)
        self.assertEqual(response.read_limit, sync.MAX_RESPONSE_BYTES + 1)
        self.assertIsInstance(build.call_args.args[0], urllib.request.ProxyHandler)
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], sync.NoRedirect)

    def test_local_commands_do_not_inherit_porkbun_credentials(self) -> None:
        command = ["openssl", "version"]
        with (
            mock.patch.dict(os.environ, {"CERT_SYNC_KEEP": "retained"}),
            mock.patch.object(
                sync.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(command, 0, b"output", b""),
            ) as run,
        ):
            self.assertEqual(sync.run_command(command), b"output")
        environment = run.call_args.kwargs["env"]
        self.assertNotIn("PORKBUN_API_KEY", environment)
        self.assertNotIn("PORKBUN_SECRET_API_KEY", environment)
        self.assertEqual(environment["CERT_SYNC_KEEP"], "retained")
        self.assertEqual(os.environ["PORKBUN_API_KEY"], "test-api-secret")

    def test_cli_defaults_to_caddy_certificate_group(self) -> None:
        with (
            mock.patch.object(sync.sys, "platform", "linux"),
            mock.patch.object(sync, "retrieve_bundle", return_value=(b"chain", b"key")),
            mock.patch.object(sync, "install_bundle", return_value=True) as install,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(sync.main([]), 0)
        self.assertEqual(install.call_args.args[4], "caddy")

    def test_redirect_handler_never_forwards_request(self) -> None:
        handler = sync.NoRedirect()
        self.assertIsNone(
            handler.redirect_request(
                urllib.request.Request("https://porkbun.com/"),
                mock.Mock(spec=http.client.HTTPResponse),
                302,
                "",
                http.client.HTTPMessage(),
                "https://elsewhere.test",
            )
        )

    def test_api_error_is_sanitized_by_cli_and_storage_is_untouched(self) -> None:
        response = FakeResponse(
            json.dumps(
                {
                    "status": "ERROR",
                    "message": "test-private-secret response-private-key",
                }
            ).encode()
        )
        opener = mock.Mock()
        opener.open.return_value = response
        error = io.StringIO()
        with (
            mock.patch.object(sync.urllib.request, "build_opener", return_value=opener),
            mock.patch.object(sync.sys, "platform", "linux"),
            mock.patch.object(sync, "install_bundle") as install,
            contextlib.redirect_stderr(error),
        ):
            self.assertEqual(sync.main([]), 1)
        install.assert_not_called()
        for secret in [
            "test-api-secret",
            "test-private-secret",
            "response-private-key",
        ]:
            self.assertNotIn(secret, error.getvalue())

    def test_network_exception_does_not_disclose_exception_text(self) -> None:
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError("test-private-secret")
        with mock.patch.object(
            sync.urllib.request, "build_opener", return_value=opener
        ):
            with self.assertRaises(sync.SyncError) as raised:
                sync.retrieve_bundle("satisshin.top")
        self.assertNotIn("test-private-secret", str(raised.exception))

    def test_rejects_oversized_and_invalid_responses(self) -> None:
        responses = [
            FakeResponse(b"", {"Content-Length": str(sync.MAX_RESPONSE_BYTES + 1)}),
            FakeResponse(b"x" * (sync.MAX_RESPONSE_BYTES + 1)),
            FakeResponse(b"[]"),
            FakeResponse(b"not json"),
            FakeResponse(
                b'{"status":"SUCCESS","certificatechain":2,"privatekey":"key"}'
            ),
        ]
        for response in responses:
            with self.subTest(response_length=len(response.data)):
                opener = mock.Mock()
                opener.open.return_value = response
                with mock.patch.object(
                    sync.urllib.request, "build_opener", return_value=opener
                ):
                    with self.assertRaises(sync.SyncError):
                        sync.retrieve_bundle("satisshin.top")

    def test_help_contains_no_environment_values(self) -> None:
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            self.assertRaises(SystemExit) as raised,
        ):
            sync.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertNotIn("test-private-secret", output.getvalue())
        self.assertIn("does not request issuance", output.getvalue())

    def test_service_name_rejects_option_and_path_injection(self) -> None:
        for name in [
            "--now.service",
            "a;touch.service",
            "../a.service",
            "a.service\nb.service",
            "a",
        ]:
            with self.subTest(name=name), self.assertRaises(Exception):
                sync.valid_service(name)
        self.assertEqual(
            sync.valid_service("llama-server@main.service"), "llama-server@main.service"
        )

    def test_reload_and_restart_options_are_mutually_exclusive(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            mock.patch.object(sync, "retrieve_bundle") as retrieve,
            self.assertRaises(SystemExit) as raised,
        ):
            sync.main(["--reload-caddy", "--restart-service", "llama-server.service"])
        self.assertEqual(raised.exception.code, 2)
        retrieve.assert_not_called()


class BundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="synthetic-bundle-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "synthetic.zip"
        self.members = [
            ("example-ssl-bundle/domain.cert.pem", b"synthetic-chain"),
            ("example-ssl-bundle/private.key.pem", b"synthetic-private-key"),
            ("example-ssl-bundle/public.key.pem", b"ignored-public-key"),
        ]

    def write_zip(self, members: list[tuple[str, bytes]]) -> None:
        with zipfile.ZipFile(
            self.path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for name, contents in members:
                archive.writestr(name, contents)

    def test_bundle_bypasses_api_and_key_environment(self) -> None:
        self.write_zip(self.members)
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(sync.sys, "platform", "linux"),
            mock.patch.object(sync, "retrieve_bundle") as retrieve,
            mock.patch.object(sync, "install_bundle", return_value=True) as install,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(sync.main(["--bundle", str(self.path)]), 0)
        retrieve.assert_not_called()
        self.assertEqual(
            install.call_args.args[1:3], (b"synthetic-chain", b"synthetic-private-key")
        )
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_missing_duplicate_and_expanded_oversize_members_are_rejected(self) -> None:
        cases = [
            self.members[:1],
            self.members + [("other/private.key.pem", b"duplicate-private-key")],
            self.members + [("ignored.txt", b"x" * (sync.MAX_RESPONSE_BYTES + 1))],
        ]
        for members in cases:
            with self.subTest(member_count=len(members)):
                self.write_zip(members)
                with self.assertRaises(sync.SyncError):
                    sync.read_bundle(self.path)

    def test_invalid_and_encrypted_archives_return_only_a_generic_error(self) -> None:
        self.write_zip(self.members)
        encrypted = bytearray(self.path.read_bytes())
        for signature, flag_offset in [(b"PK\x03\x04", 6), (b"PK\x01\x02", 8)]:
            offset = 0
            while (offset := encrypted.find(signature, offset)) != -1:
                encrypted[offset + flag_offset] |= 1
                offset += 4
        for payload in [
            b"synthetic-private-key invalid archive",
            encrypted,
            b"x" * (sync.MAX_RESPONSE_BYTES + 1),
        ]:
            self.path.write_bytes(payload)
            with self.assertRaises(sync.SyncError) as raised:
                sync.read_bundle(self.path)
            self.assertNotIn("synthetic-private-key", str(raised.exception))


@unittest.skipUnless(
    sys.platform == "linux" and shutil.which("openssl"), "requires Linux and OpenSSL"
)
class InstallationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import grp

        cls.directory = tempfile.TemporaryDirectory(prefix="certificate-tests-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.fixtures = Path(cls.directory.name)
        cls.group = grp.getgrgid(os.getgid()).gr_name
        cls.ca = cls.fixtures / "ca.pem"
        cls.ca_key = cls.fixtures / "ca.key"
        cls.openssl(
            [
                "req",
                "-x509",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:P-256",
                "-nodes",
                "-keyout",
                str(cls.ca_key),
                "-out",
                str(cls.ca),
                "-days",
                "7",
                "-subj",
                "/CN=Offline test CA",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
            ]
        )
        cls.chain, cls.key = cls.make_certificate("first", "satisshin.top", 3)
        cls.next_chain, cls.next_key = cls.make_certificate(
            "second", "satisshin.top", 3
        )
        cls.wrong_chain, cls.wrong_key = cls.make_certificate(
            "wrong", "other.example", 3
        )
        cls.short_chain, cls.short_key = cls.make_certificate(
            "short", "satisshin.top", 1
        )

    @classmethod
    def openssl(cls, arguments: list[str]) -> None:
        result = subprocess.run(
            ["openssl", *arguments], capture_output=True, timeout=30
        )
        if result.returncode:
            raise RuntimeError("Offline test certificate creation failed.")

    @classmethod
    def make_certificate(
        cls, name: str, hostname: str, days: int
    ) -> tuple[bytes, bytes]:
        key = cls.fixtures / f"{name}.key"
        csr = cls.fixtures / f"{name}.csr"
        cert = cls.fixtures / f"{name}.pem"
        extensions = cls.fixtures / f"{name}.ext"
        extensions.write_text(
            f"subjectAltName=DNS:{hostname},DNS:*.{hostname}\n"
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature\n"
            "extendedKeyUsage=serverAuth\n"
        )
        cls.openssl(
            [
                "req",
                "-new",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:P-256",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(csr),
                "-subj",
                f"/CN={hostname}",
            ]
        )
        cls.openssl(
            [
                "x509",
                "-req",
                "-in",
                str(csr),
                "-CA",
                str(cls.ca),
                "-CAkey",
                str(cls.ca_key),
                "-set_serial",
                str({"first": 1, "second": 2, "wrong": 3, "short": 4}[name]),
                "-days",
                str(days),
                "-extfile",
                str(extensions),
                "-out",
                str(cert),
            ]
        )
        return cert.read_bytes() + cls.ca.read_bytes(), key.read_bytes()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="certificate-install-")
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "tls"

    def install(
        self,
        chain: bytes | None = None,
        key: bytes | None = None,
        service: str | None = None,
        ca_file: Path | None = None,
        reload_caddy: bool = False,
    ) -> bool:
        return sync.install_bundle(
            "satisshin.top",
            self.chain if chain is None else chain,
            self.key if key is None else key,
            self.output,
            self.group,
            service,
            ca_file=self.ca if ca_file is None else ca_file,
            reload_caddy=reload_caddy,
        )

    def test_success_installs_relative_link_and_private_permissions_without_restart(
        self,
    ) -> None:
        with mock.patch.object(sync.subprocess, "run", wraps=subprocess.run) as run:
            self.assertTrue(self.install())
        self.assertTrue((self.output / "current").is_symlink())
        self.assertRegex(
            os.readlink(self.output / "current"), r"^versions/[0-9a-f]{64}$"
        )
        for path in [
            self.output,
            self.output / "versions",
            (self.output / "current").resolve(),
        ]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o750)
        for name in ["fullchain.pem", "privkey.pem"]:
            file = self.output / "current" / name
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o640)
            self.assertEqual(file.stat().st_gid, os.getgid())
        self.assertEqual(
            (self.output / "current" / "privkey.pem").read_bytes(), self.key
        )
        self.assertFalse(
            any(call.args[0][0] == "systemctl" for call in run.call_args_list)
        )
        self.assertFalse(self.install())

    def test_validation_failures_preserve_the_current_link(self) -> None:
        self.install()
        previous = os.readlink(self.output / "current")
        cases = [
            (self.wrong_chain, self.wrong_key, self.ca),
            (self.chain, self.next_key, self.ca),
            (self.short_chain, self.short_key, self.ca),
            (b"not a certificate", self.key, self.ca),
            (self.chain, self.key, self.fixtures / "does-not-exist.pem"),
        ]
        for chain, key, ca in cases:
            with self.subTest(chain_size=len(chain)), self.assertRaises(sync.SyncError):
                self.install(chain, key, ca_file=ca)
            self.assertEqual(os.readlink(self.output / "current"), previous)
        self.assertEqual(len(list((self.output / "versions").iterdir())), 1)

    def test_failed_first_validation_does_not_create_storage(self) -> None:
        with self.assertRaises(sync.SyncError):
            self.install(self.wrong_chain, self.wrong_key)
        self.assertFalse(self.output.exists())

    def test_untrusted_test_ca_is_rejected_by_system_trust(self) -> None:
        self.install()
        previous = os.readlink(self.output / "current")
        with self.assertRaises(sync.SyncError):
            sync.install_bundle(
                "satisshin.top", self.chain, self.key, self.output, self.group
            )
        self.assertEqual(os.readlink(self.output / "current"), previous)

    def test_restart_failure_rolls_back_and_retry_can_install_again(self) -> None:
        self.install()
        previous = os.readlink(self.output / "current")
        original_run = sync.run_command
        commands: list[list[str]] = []

        def failure(command: list[str], *, data: bytes | None = None) -> bytes:
            if command[0] == "systemctl":
                commands.append(command)
                raise sync.SyncError("mock failure")
            return original_run(command, data=data)

        with mock.patch.object(sync, "run_command", side_effect=failure):
            with self.assertRaisesRegex(
                sync.SyncError, "previous certificate link was restored"
            ):
                self.install(self.next_chain, self.next_key, "llama-server.service")
        self.assertEqual(os.readlink(self.output / "current"), previous)
        self.assertEqual(
            commands, [["systemctl", "try-restart", "llama-server.service"]]
        )

        def success(command: list[str], *, data: bytes | None = None) -> bytes:
            if command[0] == "systemctl":
                commands.append(command)
                return b""
            return original_run(command, data=data)

        with mock.patch.object(sync, "run_command", side_effect=success):
            self.assertTrue(
                self.install(self.next_chain, self.next_key, "llama-server.service")
            )
            self.assertFalse(
                self.install(self.next_chain, self.next_key, "llama-server.service")
            )
        self.assertEqual(len(commands), 2)
        self.assertNotEqual(os.readlink(self.output / "current"), previous)

    def test_unchanged_certificate_never_restarts_the_service(self) -> None:
        self.install()
        with mock.patch.object(sync.subprocess, "run", wraps=subprocess.run) as run:
            self.assertFalse(self.install(service="llama-server.service"))
        self.assertFalse(
            any(call.args[0][0] == "systemctl" for call in run.call_args_list)
        )

    def test_changed_certificate_reloads_caddy_once_and_unchanged_skips_reload(
        self,
    ) -> None:
        original_run = sync.run_command
        commands: list[list[str]] = []

        def success(command: list[str], *, data: bytes | None = None) -> bytes:
            if command[0] == "caddy":
                commands.append(command)
                return b""
            return original_run(command, data=data)

        with mock.patch.object(sync, "run_command", side_effect=success):
            self.assertTrue(self.install(reload_caddy=True))
            self.assertFalse(self.install(reload_caddy=True))
        self.assertEqual(
            commands,
            [
                [
                    "caddy",
                    "reload",
                    "--config",
                    "/etc/caddy/Caddyfile",
                    "--adapter",
                    "caddyfile",
                    "--force",
                ]
            ],
        )

    def test_failed_caddy_reload_restores_previous_certificate_link(self) -> None:
        self.install()
        previous = os.readlink(self.output / "current")
        original_run = sync.run_command

        def failure(command: list[str], *, data: bytes | None = None) -> bytes:
            if command[0] == "caddy":
                raise sync.SyncError("mock failure")
            return original_run(command, data=data)

        with mock.patch.object(sync, "run_command", side_effect=failure):
            with self.assertRaisesRegex(sync.SyncError, "Caddy reload failed"):
                self.install(self.next_chain, self.next_key, reload_caddy=True)
        self.assertEqual(os.readlink(self.output / "current"), previous)

    def test_first_install_restart_failure_restores_absent_link(self) -> None:
        original_run = sync.run_command

        def failure(command: list[str], *, data: bytes | None = None) -> bytes:
            if command[0] == "systemctl":
                raise sync.SyncError("mock failure")
            return original_run(command, data=data)

        with mock.patch.object(sync, "run_command", side_effect=failure):
            with self.assertRaises(sync.SyncError):
                self.install(service="llama-server.service")
        self.assertFalse((self.output / "current").exists())
        self.assertFalse((self.output / "current").is_symlink())

    def test_atomic_link_replaces_existing_link_with_one_replace(self) -> None:
        self.install()
        original = os.replace
        with mock.patch.object(sync.os, "replace", wraps=original) as replace:
            self.assertTrue(self.install(self.next_chain, self.next_key))
        replace.assert_called_once()
        self.assertEqual(replace.call_args.args[1], self.output / "current")


if __name__ == "__main__":
    unittest.main()
