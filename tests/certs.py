"""Certificates generated for the test run: an authority of our own and a server signed by it.

They live in the harness's temporary directory; the service itself writes nothing [D46].
"""

from __future__ import annotations

import datetime
import ipaddress
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

SERVER_NAME = "smtp.example.org"


@dataclass(frozen=True)
class Certificates:
    ca: Path
    cert: Path
    key: Path
    other_cert: Path
    other_key: Path


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _write_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def _server_cert(
    subject: str,
    issuer: x509.Name,
    issuer_key: ec.EllipticCurvePrivateKey,
    key: ec.EllipticCurvePrivateKey,
) -> x509.Certificate:
    now = datetime.datetime.now(datetime.UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(subject), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        # Python 3.13 verifies with VERIFY_X509_STRICT, which wants both key identifiers.
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            critical=False,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(issuer_key, hashes.SHA256())
    )


def generate(directory: Path) -> Certificates:
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = _name("mail-dispatch test authority")
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server = _server_cert(SERVER_NAME, ca_name, ca_key, server_key)
    # A certificate for another name, signed by the same authority: verification of the name
    # fails even with the authority trusted.
    other_key = ec.generate_private_key(ec.SECP256R1())
    other = _server_cert("other.example.net", ca_name, ca_key, other_key)

    paths = Certificates(
        ca=directory / "ca.pem",
        cert=directory / "server.pem",
        key=directory / "server.key",
        other_cert=directory / "other.pem",
        other_key=directory / "other.key",
    )
    paths.ca.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    paths.cert.write_bytes(server.public_bytes(serialization.Encoding.PEM))
    _write_key(paths.key, server_key)
    paths.other_cert.write_bytes(other.public_bytes(serialization.Encoding.PEM))
    _write_key(paths.other_key, other_key)
    return paths
