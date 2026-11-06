"""Check trusted TLS and hostname verification without sending an LLM request."""

from __future__ import annotations

import argparse
import socket
import ssl
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", default="satisshin.top")
    parser.add_argument("--port", type=int, default=4000)
    parser.add_argument(
        "--connect-host",
        help="TCP destination override, for example 192.168.0.11 before DNS cutover",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.timeout <= 0:
        parser.error("port must be 1..65535 and timeout must be positive")
    context = ssl.create_default_context()
    try:
        with socket.create_connection(
            (args.connect_host or args.domain, args.port), timeout=args.timeout
        ) as connection:
            with context.wrap_socket(connection, server_hostname=args.domain) as tls:
                certificate = tls.getpeercert()
                print(f"Verified https://{args.domain}:{args.port} ({tls.version()})")
                print(f"Certificate expires: {certificate.get('notAfter', 'unknown')}")
    except (OSError, ValueError) as error:
        print(f"TLS verification failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
