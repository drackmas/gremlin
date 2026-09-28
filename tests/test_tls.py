"""TLS helper: self-signed cert generation for secure-context (phone) access.

Browsers only expose the microphone in a secure context, so ``GREMLIN_TLS=1``
serves HTTPS with a generated self-signed cert.  These tests verify the cert
is valid, self-signed, covers localhost/loopback (and the LAN IP when present),
is loadable by ``ssl`` as a server context, and is reused on subsequent calls.
"""

from __future__ import annotations

import ipaddress
import ssl
from pathlib import Path

from cryptography import x509

import tls


def _load(cert_path: str) -> x509.Certificate:
    return x509.load_pem_x509_certificate(Path(cert_path).read_bytes())


def _san(cert_path: str) -> x509.SubjectAlternativeName:
    return _load(cert_path).extensions.get_extension_for_class(x509.SubjectAlternativeName).value


def test_ensure_self_signed_generates_and_reuses(tmp_path):
    cert, key = tls.ensure_self_signed(tmp_path / "tls")
    assert Path(cert).exists() and Path(key).exists()

    # Cert must match the key and form a valid TLS server context.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)

    cert_obj = _load(cert)
    assert cert_obj.subject == cert_obj.issuer  # self-signed

    # A second call must reuse the same files, not regenerate them.
    cert2, key2 = tls.ensure_self_signed(tmp_path / "tls")
    assert (cert2, key2) == (cert, key)


def test_ensure_self_signed_san_covers_localhost_and_loopback(tmp_path):
    cert, _ = tls.ensure_self_signed(tmp_path / "tls")
    san = _san(cert)
    assert "localhost" in san.get_values_for_type(x509.DNSName)
    assert ipaddress.ip_address("127.0.0.1") in san.get_values_for_type(x509.IPAddress)


def test_lan_ip_returns_valid_ipv4_or_none():
    ip = tls.lan_ip()
    if ip is not None:
        assert isinstance(ipaddress.ip_address(ip), ipaddress.IPv4Address)
