#!/usr/bin/env python3
"""Install an already-issued Porkbun certificate on a Linux server.

Porkbun must first issue the free certificate in its dashboard. This tool only
retrieves that certificate; it does not request issuance. Keep Porkbun DNS in
use so Porkbun can renew it, and run this tool periodically to install renewals.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import http.client
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
import zipfile
import zlib
from pathlib import Path
from typing import Iterator

API_BASE = "https://api.porkbun.com/api/json/v3"
MAX_RESPONSE_BYTES = 1024 * 1024
HTTP_TIMEOUT = 20
MIN_VALIDITY_SECONDS = 24 * 60 * 60
CERT_PATTERN = re.compile(
    rb"-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+"
    rb"-----END CERTIFICATE-----"
)
KEY_PATTERN = re.compile(
    rb"-----BEGIN (PRIVATE KEY|EC PRIVATE KEY|RSA PRIVATE KEY)-----\r?\n"
    rb"[A-Za-z0-9+/=\r\n]+-----END \1-----\s*"
)
SERVICE_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*\.service\Z")


class SyncError(Exception):
    """An operational failure whose message contains no response or key data."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward API credentials to a redirect destination."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: http.client.HTTPResponse,
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> None:
        return None


def valid_domain(value: str) -> str:
    value = value.lower()
    labels = value.split(".")
    if (
        len(value) > 253
        or len(labels) < 2
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        )
    ):
        raise argparse.ArgumentTypeError("Use an ASCII DNS domain name.")
    return value


def valid_service(value: str) -> str:
    if not SERVICE_PATTERN.fullmatch(value) or len(value) > 255:
        raise argparse.ArgumentTypeError("Use a service unit name ending in .service.")
    return value


def retrieve_bundle(domain: str) -> tuple[bytes, bytes]:
    """Download an existing certificate, without requesting a new one."""
    api_key = os.environ.get("PORKBUN_API_KEY", "")
    secret_key = os.environ.get("PORKBUN_SECRET_API_KEY", "")
    if not api_key.strip() or not secret_key.strip():
        raise SyncError("Set PORKBUN_API_KEY and PORKBUN_SECRET_API_KEY.")
    request = urllib.request.Request(
        f"{API_BASE}/ssl/retrieve/{domain}",
        data=json.dumps({"apikey": api_key, "secretapikey": secret_key}).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        with opener.open(request, timeout=HTTP_TIMEOUT) as response:
            if response.status != 200:
                raise SyncError("Porkbun did not return a certificate bundle.")
            length = response.headers.get("Content-Length")
            if length is not None and int(length) > MAX_RESPONSE_BYTES:
                raise SyncError("Porkbun response exceeds the size limit.")
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except SyncError:
        raise
    except (OSError, ValueError, http.client.HTTPException, urllib.error.URLError):
        raise SyncError(
            "Porkbun request failed; no certificate files were changed."
        ) from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise SyncError("Porkbun response exceeds the size limit.")
    try:
        result = json.loads(body)
        if not isinstance(result, dict) or result.get("status") != "SUCCESS":
            raise ValueError
        chain, key = result["certificatechain"], result["privatekey"]
        if (
            not isinstance(chain, str)
            or not isinstance(key, str)
            or not chain
            or not key
        ):
            raise ValueError
        return chain.encode("ascii"), key.encode("ascii")
    except (KeyError, ValueError, TypeError, UnicodeError, RecursionError):
        raise SyncError(
            "Porkbun returned no usable certificate; issuance may still be pending."
        ) from None


def read_bundle(path: Path) -> tuple[bytes, bytes]:
    """Read a downloaded Porkbun ZIP in memory without extracting any paths."""
    try:
        with path.open("rb") as handle:
            payload = handle.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ValueError
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            if (
                sum(member.file_size for member in members) > MAX_RESPONSE_BYTES
                or len({member.filename for member in members}) != len(members)
                or any(
                    member.flag_bits & 1
                    or member.compress_type
                    not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    for member in members
                )
            ):
                raise ValueError
            contents: list[bytes] = []
            for basename in ("domain.cert.pem", "private.key.pem"):
                matches = [
                    member
                    for member in members
                    if not member.is_dir()
                    and member.filename.rsplit("/", 1)[-1] == basename
                ]
                if len(matches) != 1:
                    raise ValueError
                with archive.open(matches[0]) as member:
                    data = member.read(MAX_RESPONSE_BYTES + 1)
                if not data or len(data) > MAX_RESPONSE_BYTES:
                    raise ValueError
                contents.append(data)
            if sum(map(len, contents)) > MAX_RESPONSE_BYTES:
                raise ValueError
            return contents[0], contents[1]
    except (
        OSError,
        ValueError,
        EOFError,
        RuntimeError,
        NotImplementedError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        zlib.error,
    ):
        raise SyncError(
            "The downloaded SSL bundle is invalid or exceeds the size limit."
        ) from None


def run_command(command: list[str], *, data: bytes | None = None) -> bytes:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in {"PORKBUN_API_KEY", "PORKBUN_SECRET_API_KEY"}
    }
    try:
        result = subprocess.run(
            command,
            input=data,
            capture_output=True,
            timeout=30,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        raise SyncError("A required local command failed.") from None
    if result.returncode:
        raise SyncError("A required local command failed.")
    return result.stdout


def validate_bundle(
    domain: str,
    chain: bytes,
    key: bytes,
    *,
    ca_file: Path | None = None,
) -> tuple[bytes, bytes]:
    """Verify trust, server purpose, hostname, validity and the private key."""
    if len(chain) + len(key) > MAX_RESPONSE_BYTES:
        raise SyncError("Certificate bundle exceeds the size limit.")
    certificates = CERT_PATTERN.findall(chain)
    if (
        not certificates
        or len(certificates) > 10
        or CERT_PATTERN.sub(b"", chain).strip()
        or not KEY_PATTERN.fullmatch(key.strip())
    ):
        raise SyncError("Certificate bundle has an invalid PEM format.")
    chain = b"\n".join(certificates) + b"\n"
    key = key.strip() + b"\n"
    with tempfile.TemporaryDirectory(prefix="porkbun-verify-") as temporary:
        directory = Path(temporary)
        leaf = directory / "leaf.pem"
        intermediates = directory / "intermediates.pem"
        private = directory / "private.pem"
        leaf.write_bytes(certificates[0] + b"\n")
        private.write_bytes(key)
        private.chmod(0o600)
        command = [
            "openssl",
            "verify",
            "-purpose",
            "sslserver",
            "-verify_hostname",
            domain,
        ]
        if ca_file is not None:
            command.extend(["-CAfile", str(ca_file)])
        if len(certificates) > 1:
            intermediates.write_bytes(b"\n".join(certificates[1:]) + b"\n")
            command.extend(["-untrusted", str(intermediates)])
        command.append(str(leaf))
        try:
            run_command(command)
            run_command(
                [
                    "openssl",
                    "x509",
                    "-in",
                    str(leaf),
                    "-noout",
                    "-checkend",
                    str(MIN_VALIDITY_SECONDS),
                ]
            )
            public_pem = run_command(
                ["openssl", "x509", "-in", str(leaf), "-pubkey", "-noout"]
            )
            certificate_public = run_command(
                ["openssl", "pkey", "-pubin", "-outform", "DER"], data=public_pem
            )
            key_public = run_command(
                ["openssl", "pkey", "-in", str(private), "-pubout", "-outform", "DER"]
            )
            if not hmac.compare_digest(certificate_public, key_public):
                raise SyncError("Private key does not match the certificate.")
        except SyncError:
            raise SyncError(
                "Certificate validation failed (trust, hostname, validity or key)."
            ) from None
    return chain, key


def ensure_directory(path: Path, group_id: int) -> None:
    """Create new parents privately; leave permissions of existing parents alone."""
    if not path.exists() and not path.is_symlink():
        if not path.parent.exists():
            ensure_directory(path.parent, group_id)
        path.mkdir(mode=0o750)
    if path.is_symlink() or not path.is_dir():
        raise SyncError("Certificate storage must contain real directories.")
    path.chmod(0o750)
    os.chown(path, -1, group_id)


@contextlib.contextmanager
def storage_lock(directory: Path) -> Iterator[None]:
    import fcntl

    descriptor = os.open(
        directory / ".sync.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def atomic_link(directory: Path, target: str) -> None:
    temporary = directory / f".current-{uuid.uuid4().hex}"
    try:
        temporary.symlink_to(target)
        os.replace(temporary, directory / "current")
    finally:
        temporary.unlink(missing_ok=True)


def install_bundle(
    domain: str,
    chain: bytes,
    key: bytes,
    output_dir: Path,
    group: str,
    restart_service: str | None = None,
    *,
    ca_file: Path | None = None,
    reload_caddy: bool = False,
) -> bool:
    """Install validated files, optionally reloading Caddy or restarting a service.

    An unchanged certificate never triggers a reload or restart. On activation
    failure the previous link is restored, so the next sync can retry the change.
    """
    import grp

    domain = valid_domain(domain)
    if reload_caddy and restart_service is not None:
        raise SyncError("Choose either a Caddy reload or a service restart.")
    if restart_service is not None:
        restart_service = valid_service(restart_service)
    chain, key = validate_bundle(domain, chain, key, ca_file=ca_file)
    try:
        group_id = grp.getgrnam(group).gr_gid
    except KeyError:
        raise SyncError("The configured certificate group does not exist.") from None
    ensure_directory(output_dir, group_id)
    versions = output_dir / "versions"
    ensure_directory(versions, group_id)
    fingerprint = hashlib.sha256(chain + b"\0" + key).hexdigest()
    target = f"versions/{fingerprint}"
    with storage_lock(output_dir):
        current = output_dir / "current"
        if current.is_symlink():
            previous = os.readlink(current)
            if not re.fullmatch(r"versions/[0-9a-f]{64}", previous):
                raise SyncError(
                    "The current certificate link has an unexpected target."
                )
        elif current.exists():
            raise SyncError("The current certificate path must be a symbolic link.")
        else:
            previous = None
        destination = versions / fingerprint
        expected = {"fullchain.pem": chain, "privkey.pem": key}
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_dir():
                raise SyncError("Stored certificate version is invalid.")
            for name, contents in expected.items():
                file = destination / name
                if (
                    file.is_symlink()
                    or not file.is_file()
                    or file.read_bytes() != contents
                ):
                    raise SyncError("Stored certificate version is invalid.")
                file.chmod(0o640)
                os.chown(file, -1, group_id)
            ensure_directory(destination, group_id)
        else:
            staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=versions))
            try:
                staging.chmod(0o750)
                os.chown(staging, -1, group_id)
                for name, contents in expected.items():
                    descriptor = os.open(
                        staging / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640
                    )
                    with os.fdopen(descriptor, "wb") as handle:
                        os.fchmod(handle.fileno(), 0o640)
                        os.fchown(handle.fileno(), -1, group_id)
                        handle.write(contents)
                        handle.flush()
                        os.fsync(handle.fileno())
                os.rename(staging, destination)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        changed = previous != target
        if changed:
            atomic_link(output_dir, target)
        if changed and (reload_caddy or restart_service is not None):
            activation = "Caddy reload" if reload_caddy else "Service restart"
            if reload_caddy:
                command = [
                    "caddy",
                    "reload",
                    "--config",
                    "/etc/caddy/Caddyfile",
                    "--adapter",
                    "caddyfile",
                    "--force",
                ]
            else:
                assert restart_service is not None
                command = ["systemctl", "try-restart", restart_service]
            try:
                run_command(command)
            except SyncError:
                try:
                    if previous is None:
                        current.unlink(missing_ok=True)
                    else:
                        atomic_link(output_dir, previous)
                except OSError:
                    raise SyncError(
                        f"{activation} failed and the previous link could not be restored."
                    ) from None
                raise SyncError(
                    f"{activation} failed; the previous certificate link was restored."
                ) from None
        return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", type=valid_domain, default="satisshin.top")
    parser.add_argument(
        "--bundle",
        type=Path,
        metavar="PATH",
        help="Read a downloaded Porkbun SSL ZIP instead of using the API; no API keys required.",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("/etc/satisshin/tls"))
    parser.add_argument(
        "--group", default="caddy", help="Group allowed to read the certificate key."
    )
    activation = parser.add_mutually_exclusive_group()
    activation.add_argument(
        "--restart-service",
        type=valid_service,
        metavar="NAME.service",
        help="When the certificate changes, try-restart this service; inactive services "
        "stay inactive. Unchanged certificates never restart it. Default: no restart.",
    )
    activation.add_argument(
        "--reload-caddy",
        action="store_true",
        help="When the certificate changes, reload /etc/caddy/Caddyfile with Caddy. "
        "Unchanged certificates never reload it. Default: no reload.",
    )
    arguments = parser.parse_args(argv)
    if sys.platform != "linux":
        parser.exit(1, "This tool requires Linux.\n")
    try:
        chain, key = (
            read_bundle(arguments.bundle)
            if arguments.bundle is not None
            else retrieve_bundle(arguments.domain)
        )
        changed = install_bundle(
            arguments.domain,
            chain,
            key,
            arguments.output_dir,
            arguments.group,
            arguments.restart_service,
            reload_caddy=arguments.reload_caddy,
        )
    except (SyncError, OSError) as error:
        # OSError paths and external command output can contain sensitive data.
        message = (
            str(error)
            if isinstance(error, SyncError)
            else "Local certificate installation failed."
        )
        print(f"Certificate sync failed: {message}", file=sys.stderr)
        return 1
    print(
        "Certificate files updated."
        if changed
        else "Certificate files already current."
    )
    if changed and arguments.restart_service:
        print("Service try-restart completed.")
    if changed and arguments.reload_caddy:
        print("Caddy reload completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
