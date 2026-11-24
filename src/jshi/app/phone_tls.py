"""Issue one LAN server certificate. The temporary issuer key is discarded after signing."""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory


def local_certificate(root: Path, ip: str) -> tuple[Path, Path, Path]:
    address = ipaddress.ip_address(ip)
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = root / address.exploded.replace(':', '-')
    cert, key, ca = dest/'server.pem', dest/'server.key', dest/'iphone-ca.cer'
    if not dest.exists():
        openssl = shutil.which('openssl')
        if openssl is None:
            raise ValueError('自动签发需要 OpenSSL，也可以指定 --phone-cert 和 --phone-key。')
        with TemporaryDirectory(dir=root) as temp:
            tmp = Path(temp)
            bundle = tmp/'bundle'
            bundle.mkdir(mode=0o700)
            config = tmp/'openssl.cnf'
            config.write_text(f"""[req]
prompt = no
distinguished_name = name
[name]
CN = JShi Device
[ca]
basicConstraints = critical,CA:TRUE,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
[server]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = IP:{address}
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
""", encoding='ascii')
            def run(*args):
                result = subprocess.run([openssl, *args], cwd=tmp, capture_output=True, timeout=30)
                if result.returncode:
                    raise ValueError('OpenSSL 签发失败：'+result.stderr.decode(errors='replace')[-400:])
            run('req','-new','-x509','-newkey','rsa:2048','-nodes','-sha256','-days','730',
                '-config',str(config),'-extensions','ca','-subj','/CN=JShi Phone Local CA',
                '-keyout','ca.key','-out','ca.pem')
            run('req','-new','-newkey','rsa:2048','-nodes','-sha256','-config',str(config),
                '-subj',f'/CN={address}','-keyout','bundle/server.key','-out','server.csr')
            run('x509','-req','-in','server.csr','-CA','ca.pem','-CAkey','ca.key','-CAcreateserial',
                '-days','365','-sha256','-extfile',str(config),'-extensions','server','-out','bundle/server.pem')
            run('x509','-in','ca.pem','-outform','DER','-out','bundle/iphone-ca.cer')
            (bundle/'server.key').chmod(0o600)
            try:
                os.rename(bundle, dest)
            except OSError:
                if not dest.is_dir():
                    raise
    if not all(p.is_file() for p in (cert,key,ca)):
        raise ValueError(f'设备证书目录不完整，请将 {dest} 移走后重新启动。')
    return cert, key, ca
