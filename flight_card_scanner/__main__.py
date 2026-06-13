"""Entry point for running the Flight Card Scanner with uvicorn.

Usage:
    .venv/bin/python -m flight_card_scanner

Reads host/port from config.json (or CONFIG_PATH env var) and starts
uvicorn bound to that address. Optionally enables HTTPS if ssl_certfile
and ssl_keyfile are configured, readable, and not expired.
"""

import os
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path

import uvicorn

from .config import load_config

config_path = Path(os.environ.get("CONFIG_PATH", "config.json"))
config = load_config(config_path)


def _check_ssl(config) -> dict:
    """Validate SSL configuration and return uvicorn ssl kwargs.

    Returns a dict with ssl_certfile/ssl_keyfile if valid, or empty dict.
    Prints clear status messages about SSL state.
    """
    if config.ssl_certfile is None or config.ssl_keyfile is None:
        print("INFO:     SSL not configured (ssl_certfile/ssl_keyfile not set in config)")
        return {}

    certfile = config.ssl_certfile
    keyfile = config.ssl_keyfile

    # Check files exist and are readable
    if not certfile.exists():
        print(f"WARNING:  SSL disabled — certificate file not found: {certfile}")
        return {}
    if not keyfile.exists():
        print(f"WARNING:  SSL disabled — key file not found: {keyfile}")
        return {}

    if not os.access(certfile, os.R_OK):
        print(f"WARNING:  SSL disabled — certificate file not readable: {certfile}")
        return {}
    if not os.access(keyfile, os.R_OK):
        print(f"WARNING:  SSL disabled — key file not readable: {keyfile}")
        return {}

    # Check certificate expiration
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))

        # Parse the cert to check expiry
        import cryptography.x509
        from cryptography.hazmat.primitives.serialization import Encoding

        cert_pem = certfile.read_bytes()
        cert = cryptography.x509.load_pem_x509_certificate(cert_pem)
        not_after = cert.not_valid_after_utc

        now = datetime.now(timezone.utc)
        if now > not_after:
            print(f"WARNING:  SSL disabled — certificate expired on {not_after.strftime('%Y-%m-%d %H:%M UTC')}")
            print(f"          Run 'tailscale cert <hostname>' to renew")
            return {}

        days_remaining = (not_after - now).days
        if days_remaining < 7:
            print(f"INFO:     SSL certificate expires in {days_remaining} days — consider renewing soon")
            print(f"          Run 'tailscale cert <hostname>' to renew")

    except ImportError:
        # cryptography not installed — skip expiry check, trust the files
        print("INFO:     Cannot check certificate expiry (cryptography package not installed)")
    except Exception as exc:
        print(f"WARNING:  SSL disabled — error loading certificate: {exc}")
        return {}

    print(f"INFO:     SSL enabled — using cert: {certfile}")
    return {
        "ssl_certfile": str(certfile),
        "ssl_keyfile": str(keyfile),
    }


if __name__ == "__main__":
    ssl_kwargs = _check_ssl(config)

    if ssl_kwargs:
        print(f"INFO:     Starting HTTPS server on {config.host}:{config.port}")
    else:
        print(f"INFO:     Starting HTTP server on {config.host}:{config.port}")

    uvicorn.run(
        "flight_card_scanner.main:app",
        host=config.host,
        port=config.port,
        log_level="info",
        **ssl_kwargs,
    )
