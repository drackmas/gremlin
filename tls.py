"""Optional HTTPS support (self-signed certificate).

Browsers only expose the microphone (``navigator.mediaDevices``) in a secure
context — ``https://`` or ``localhost``.  Desktop users are fine on
``http://127.0.0.1``, but phones reaching the app over the LAN get
``http://<lan-ip>`` and the mic silently disappears.  ``GREMLIN_TLS=1`` (or
explicit ``GREMLIN_SSL_CERT``/``GREMLIN_SSL_KEY``) turns on HTTPS so phone
access gets a secure context; the self-signed cert must be accepted once in
the browser.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

log = logging.getLogger("gremlin.tls")


def lan_ip() -> str | None:
    """Best-effort LAN IP: connect a UDP socket (no packet is sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def ensure_self_signed(directory: Path, common_name: str = "gremlin", days: int = 3650) -> tuple[str, str]:
    """Return ``(cert_path, key_path)``; generate a self-signed cert if missing.

    The certificate covers ``localhost``, ``127.0.0.1`` and the machine's
    LAN IP so the phone can reach it directly.  The key is written
    unencrypted (local-only helper; the cert is self-signed anyway).
    """
    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / "gremlin.crt"
    key_path = directory / "gremlin.key"
    if cert_path.exists() and key_path.exists():
        return str(cert_path), str(key_path)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    san: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
    ]
    ip = lan_ip()
    if ip:
        san.append(x509.IPAddress(ipaddress.IPv4Address(ip)))

    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))  # tolerate clock skew
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log.info(
        "generated self-signed TLS cert for %s (SAN: %s)",
        common_name,
        ", ".join(n.value if isinstance(n, x509.DNSName) else str(n.value) for n in san),
    )
    return str(cert_path), str(key_path)
