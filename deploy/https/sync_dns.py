#!/usr/bin/env python3
"""Update exactly one existing apex A record using a server's public IPv4.

Dry-run is the default: only --apply permits a write. No records are created or
deleted. Run on the endpoint server, with PORKBUN_API_KEY and
PORKBUN_SECRET_API_KEY supplied in its environment. Automatic discovery requires
agreement between ipify and AWS checkip over HTTPS; environment HTTP proxies are
disabled. --ipv4 overrides discovery, but still requires a global IPv4 address.

API contract: https://porkbun.com/api/json/v3/spec
IP sources: https://www.ipify.org/ and AWS's https://checkip.amazonaws.com/
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any

API_BASE = "https://api.porkbun.com/api/json/v3"
IP_SOURCES = ("https://api.ipify.org", "https://checkip.amazonaws.com/")
HTTP_TIMEOUT = 20
MAX_RESPONSE_BYTES = 64 * 1024
IP_RESPONSE_BYTES = 128
TTL = 600
# Conservatively reject protocol/deprecated ranges whose ipaddress classification
# differs between supported Python versions, as well as ordinary special ranges.
SPECIAL_USE = tuple(
    ipaddress.IPv4Network(network)
    for network in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)


class SyncError(Exception):
    """A public error that never contains credentials or response contents."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
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
    domain = value.lower()
    labels = domain.split(".")
    if (
        len(domain) > 253
        or len(labels) < 2
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        )
    ):
        raise argparse.ArgumentTypeError("Use an ASCII DNS domain name.")
    return domain


def public_ipv4(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value.strip())
    except ipaddress.AddressValueError:
        raise SyncError("A global public IPv4 address is required.") from None
    if not address.is_global or any(address in network for network in SPECIAL_USE):
        raise SyncError("A global public IPv4 address is required.")
    return str(address)


def request_bytes(
    url: str,
    *,
    data: bytes | None = None,
    limit: int = MAX_RESPONSE_BYTES,
) -> bytes:
    headers = {"Accept": "application/json" if data is not None else "text/plain"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data is not None else "GET"
    )
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        with opener.open(request, timeout=HTTP_TIMEOUT) as response:
            if response.status != 200:
                raise SyncError("An HTTPS request failed.")
            length = response.headers.get("Content-Length")
            if length is not None and int(length) > limit:
                raise SyncError("An HTTPS response exceeds the size limit.")
            body = response.read(limit + 1)
    except SyncError:
        raise
    except (OSError, ValueError, http.client.HTTPException, urllib.error.URLError):
        raise SyncError("An HTTPS request failed.") from None
    if len(body) > limit:
        raise SyncError("An HTTPS response exceeds the size limit.")
    return body


def discover_ipv4() -> str:
    addresses = []
    for source in IP_SOURCES:
        body = request_bytes(source, limit=IP_RESPONSE_BYTES)
        try:
            addresses.append(public_ipv4(body.decode("ascii")))
        except UnicodeError:
            raise SyncError("An IP source returned an invalid response.") from None
    if len(set(addresses)) != 1:
        raise SyncError(
            "The two public IPv4 sources disagree; no DNS write is allowed."
        )
    return addresses[0]


def api_request(
    path: str, fields: dict[str, str | int] | None = None
) -> dict[str, Any]:
    api_key = os.environ.get("PORKBUN_API_KEY", "")
    secret_key = os.environ.get("PORKBUN_SECRET_API_KEY", "")
    if not api_key.strip() or not secret_key.strip():
        raise SyncError("Set PORKBUN_API_KEY and PORKBUN_SECRET_API_KEY.")
    data = json.dumps(
        {
            **(fields or {}),
            "apikey": api_key,
            "secretapikey": secret_key,
        }
    ).encode()
    body = request_bytes(f"{API_BASE}/{path}", data=data)
    try:
        result = json.loads(body)
        if not isinstance(result, dict) or result.get("status") != "SUCCESS":
            raise ValueError
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise SyncError("Porkbun returned no usable DNS response.") from None
    return result


def read_apex_record(domain: str) -> tuple[str, str, int]:
    result = api_request(f"dns/retrieveByNameType/{domain}/A")
    records = result.get("records")
    if not isinstance(records, list) or len(records) != 1:
        raise SyncError(
            "Exactly one existing apex A record is required; no records were selected."
        )
    record = records[0]
    if not isinstance(record, dict):
        raise SyncError("Porkbun returned an invalid DNS record.")
    record_id, name = record.get("id"), record.get("name")
    content, ttl = record.get("content"), record.get("ttl")
    if (
        not isinstance(record_id, str)
        or not re.fullmatch(r"[0-9]{1,32}", record_id)
        or not isinstance(name, str)
        or name.lower().removesuffix(".") != domain
        or record.get("type") != "A"
        or not isinstance(content, str)
        or isinstance(ttl, bool)
        or not re.fullmatch(r"[0-9]{1,10}", str(ttl))
    ):
        raise SyncError("Porkbun returned an unexpected apex A record.")
    return record_id, public_ipv4(content), int(ttl)


def sync_dns(domain: str, *, ipv4: str | None = None, apply: bool = False) -> str:
    domain = valid_domain(domain)
    desired = discover_ipv4() if ipv4 is None else public_ipv4(ipv4)
    record_id, current, _ = read_apex_record(domain)
    if current == desired:
        return "DNS A record already current; no write performed."
    if not apply:
        return (
            "Dry-run: the existing apex A record would be updated; no write performed."
        )
    result = api_request(
        f"dns/edit/{domain}/{record_id}", {"content": desired, "ttl": TTL}
    )
    verified_id, verified_ip, verified_ttl = read_apex_record(domain)
    if (verified_id, verified_ip, verified_ttl) != (record_id, desired, TTL):
        raise SyncError(
            "DNS update could not be verified; inspect the existing record."
        )
    if result.get("warnings"):
        raise SyncError("Porkbun reported a DNS warning; the update requires review.")
    return "DNS A record updated and verified."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", type=valid_domain, default="satisshin.top")
    parser.add_argument(
        "--ipv4", metavar="IPv4", help="Explicit public IPv4; skip discovery."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Allow updating the existing apex A record.",
    )
    arguments = parser.parse_args(argv)
    if sys.platform != "linux":
        parser.exit(1, "This tool requires Linux.\n")
    try:
        message = sync_dns(arguments.domain, ipv4=arguments.ipv4, apply=arguments.apply)
    except (SyncError, OSError) as error:
        message = (
            str(error)
            if isinstance(error, SyncError)
            else "A local DNS sync operation failed."
        )
        print(f"DNS sync failed: {message}", file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
