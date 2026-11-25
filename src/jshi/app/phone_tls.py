"""Issue one LAN server certificate. The temporary issuer key is discarded after signing."""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from tempfile import TemporaryDirectory


def local_certificate(root: Path, ip: str) -> tuple[Path, Path, Path]:
    address = ipaddress.ip_address(ip)
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = root / address.exploded.replace(':', '-')
    cert, key, ca = dest/'server.pem', dest/'server.key', dest/'iphone-ca.cer'
    if not dest.exists():
        with TemporaryDirectory(dir=root) as temp:
            tmp = Path(temp)
            bundle = tmp/'bundle'
            bundle.mkdir(mode=0o700)
            _sign(bundle, address)
            (bundle/'server.key').chmod(0o600)
            try:
                os.rename(bundle, dest)
            except OSError:
                if not dest.is_dir():
                    raise
    if not all(p.is_file() for p in (cert,key,ca)):
        raise ValueError(f'设备证书目录不完整，请将 {dest} 移走后重新启动。')
    return cert, key, ca


def _sign(bundle, address):
    """Build the same trust chain on a host without an openssl executable."""
    from datetime import datetime, timedelta, timezone
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError as exc:
        raise ValueError('自动签发需要 cryptography，请在匠石环境安装项目的 vision 依赖。') from exc

    now = datetime.now(timezone.utc)
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'JShi Phone Local CA')])
    issuer_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def certificate(subject, key, days, *, ca):
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=days))
                .add_extension(x509.BasicConstraints(ca=ca, path_length=0 if ca else None), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=not ca, content_commitment=False,
                    key_encipherment=not ca, data_encipherment=False, key_agreement=False,
                    key_cert_sign=ca, crl_sign=ca, encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False))

    root = certificate(issuer, issuer_key, 730, ca=True).sign(issuer_key, hashes.SHA256())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(address))])
    leaf = (certificate(subject, server_key, 365, ca=False)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(address)]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False)
            .sign(issuer_key, hashes.SHA256()))
    (bundle/'iphone-ca.cer').write_bytes(root.public_bytes(serialization.Encoding.DER))
    (bundle/'server.pem').write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    (bundle/'server.key').write_bytes(server_key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
