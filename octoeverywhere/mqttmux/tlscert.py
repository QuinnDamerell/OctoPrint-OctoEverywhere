import ipaddress
import logging
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
from typing import List, Optional, Tuple


_CertificateLifetimeDays = 365
_RenewBeforeSeconds = 30 * 24 * 60 * 60
_OpenSslTimeoutSeconds = 60


def CreateServerTlsContext(logger: logging.Logger, storage_dir: str,
                           cert_path: Optional[str] = None,
                           key_path: Optional[str] = None,
                           bind: Optional[str] = None) -> ssl.SSLContext:
    """Load custom credentials or persist a unique, automatically renewed local certificate.

    Custom certificates are owned by the administrator and are never modified. A
    custom cert_path may contain both the certificate chain and its private key.
    Automatically generated certificates are self-signed, so clients must trust
    them explicitly or support disabling server certificate verification.
    """
    if key_path and not cert_path:
        raise ValueError("Local MQTT TLS key_path requires cert_path")
    if cert_path:
        return _LoadContext(cert_path, key_path)

    directory = os.path.join(storage_dir, "mqtt-tls")
    if os.path.islink(directory):
        raise ValueError("Local MQTT TLS storage directory must not be a symlink")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    _TryRestrictPermissions(logger, directory, 0o700)
    pem_path = os.path.join(directory, "server.pem")
    if os.path.islink(pem_path):
        raise ValueError("Local MQTT TLS credentials must not be a symlink")

    openssl = shutil.which("openssl")
    # Entware tools on Creality devices aren't always on the service's PATH.
    if openssl is None and os.path.isfile("/opt/bin/openssl"):
        openssl = "/opt/bin/openssl"
    if openssl is None:
        raise RuntimeError("Local MQTT TLS needs the OpenSSL command-line tool to create and renew "
                           "its certificate. Update the installation or install openssl "
                           "(openssl-util on OpenWrt), or configure a custom TLS certificate and key.")

    existing_context = None
    if os.path.isfile(pem_path):
        _TryRestrictPermissions(logger, pem_path, 0o600)
        try:
            existing_context = _LoadContext(pem_path)
        except (OSError, ValueError):
            logger.warning("Local MQTT TLS credentials could not be loaded; generating a replacement.")
        # Preserve client trust across hostname changes, such as Docker container
        # recreation. A usable certificate is replaced only when renewal is due;
        # changing the hostname or bind address must not silently rotate it.
        if existing_context is not None and _CertificateIsCurrent(openssl, pem_path):
            _ExportPublicCertificate(pem_path)
            return existing_context

    # A single PEM makes replacement atomic: readers can never observe a new
    # certificate paired with an old key. All intermediate files stay private.
    with tempfile.TemporaryDirectory(prefix=".generate-", dir=directory) as temporary:
        config_path = os.path.join(temporary, "openssl.cnf")
        with open(config_path, "w", encoding="ascii") as config_file:
            config_file.write(_CertificateConfig(bind))
        generated_cert = os.path.join(temporary, "certificate.pem")
        generated_key = os.path.join(temporary, "key.pem")
        args = ["req", "-new", "-x509", "-sha256", "-days", str(_CertificateLifetimeDays),
                "-set_serial", "0x" + secrets.token_hex(16), "-config", config_path,
                "-out", generated_cert]
        if existing_context is not None:
            # Keep the installation's identity when renewing its certificate.
            # Clients pinning the full certificate still need to trust the renewal.
            args.extend(["-key", pem_path])
            generated_key = pem_path
        else:
            args.extend(["-newkey", "rsa:2048", "-nodes", "-keyout", generated_key])
        _RunOpenSsl(openssl, args)

        combined_path = os.path.join(temporary, "server.pem")
        # mkstemp isn't needed here: the containing directory is private and new.
        with open(combined_path, "wb") as combined:
            os.chmod(combined_path, 0o600)
            with open(generated_cert, "rb") as certificate:
                combined.write(certificate.read())
            with open(generated_key, "rb") as private_key:
                # Renewals read a combined PEM; retain only its private-key block.
                key_data = private_key.read()
            match = re.search(br"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----.*?"
                              br"-----END (?:RSA |EC )?PRIVATE KEY-----", key_data, re.DOTALL)
            if match is None:
                raise ValueError("OpenSSL did not produce a usable local MQTT TLS private key")
            combined.write(match.group(0) + b"\n")
            combined.flush()
            os.fsync(combined.fileno())
        context = _LoadContext(combined_path)
        os.replace(combined_path, pem_path)

    _ExportPublicCertificate(pem_path)

    logger.info("Local MQTT TLS %s its self-signed certificate at %s; clients must trust the certificate "
                "or support disabling certificate verification.",
                "renewed" if existing_context is not None else "created", pem_path)
    return context


def _LoadContext(cert_path: str, key_path: Optional[str] = None) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.minimum_version = ssl.TLSVersion.TLSv1_2
    except (AttributeError, NotImplementedError, ValueError):
        # Python 3.7 on older embedded OpenSSL builds lacks minimum_version.
        context.options |= ssl.OP_NO_SSLv2 | ssl.OP_NO_SSLv3 | ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
    # Providing a password callback prevents an encrypted custom key from opening
    # an interactive terminal prompt in a headless plugin process.
    context.load_cert_chain(cert_path, key_path, password=lambda: "")
    return context


def _RunOpenSsl(executable: str, args: List[str], allow_expired: bool = False) -> "subprocess.CompletedProcess[bytes]":
    result = subprocess.run([executable] + args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=_OpenSslTimeoutSeconds, check=False)
    if result.returncode != 0 and not (allow_expired and result.returncode == 1):
        # Never include PEM material or subprocess output in logs/errors.
        raise RuntimeError("OpenSSL " + args[0] + " failed while preparing local MQTT TLS credentials "
                           "(exit code " + str(result.returncode) + ")")
    return result


def _CertificateIsCurrent(executable: str, path: str) -> bool:
    result = _RunOpenSsl(executable, ["x509", "-in", path, "-checkend", str(_RenewBeforeSeconds), "-noout"],
                         allow_expired=True)
    return result.returncode == 0


def _ExportPublicCertificate(pem_path: str) -> None:
    # Export a separate, public-only file that is safe to copy into client trust
    # stores. Never direct users to share the combined PEM containing the key.
    with open(pem_path, "rb") as credentials:
        match = re.search(br"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
                          credentials.read(), re.DOTALL)
    if match is None:
        raise ValueError("Missing certificate in local MQTT TLS credentials")
    _WriteFileAtomically(os.path.join(os.path.dirname(pem_path), "server.crt"), match.group(0) + b"\n")


def _TryRestrictPermissions(logger: logging.Logger, path: str, mode: int) -> None:
    # Files created by another user (e.g. an earlier root-run container) can't be
    # chmod-ed, but are still usable if readable. Don't let that disable TLS.
    try:
        os.chmod(path, mode)
    except OSError as e:
        logger.debug("Local MQTT TLS could not restrict permissions on %s: %s", path, e)


def _WriteFileAtomically(path: str, data: bytes) -> None:
    descriptor, temporary_path = tempfile.mkstemp(prefix=".write-", dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


def _SubjectAltNames(bind: Optional[str]) -> Tuple[List[str], List[str]]:
    names = ["localhost"]
    addresses = ["127.0.0.1", "::1"]
    for host in (socket.gethostname(), bind):
        if not host:
            continue
        try:
            address = ipaddress.ip_address(host)
            if not address.is_unspecified and str(address) not in addresses:
                addresses.append(str(address))
        except ValueError:
            # Avoid both OpenSSL config interpolation and multiline injection.
            if len(host) <= 253 and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
                if host not in names:
                    names.append(host)
    return names, addresses


def _CertificateConfig(bind: Optional[str]) -> str:
    # Use a config file rather than `req -addext`, which older OpenSSL versions
    # (including those shipped on the Sonic Pad) do not support.
    names, addresses = _SubjectAltNames(bind)
    # Different relays must not share an issuer/subject name: some trust stores
    # otherwise select the wrong self-signed certificate from a shared bundle.
    subject = "OctoEverywhere MQTT " + secrets.token_hex(16)
    config = ("[req]\nprompt = no\ndistinguished_name = subject\nx509_extensions = server\n"
              "[subject]\nCN = " + subject + "\n"
              "[server]\nbasicConstraints = critical,CA:FALSE\n"
              "keyUsage = critical,digitalSignature,keyEncipherment\n"
              "extendedKeyUsage = serverAuth\nsubjectAltName = @names\n[names]\n")
    for index, name in enumerate(names, 1):
        config += "DNS." + str(index) + " = " + name + "\n"
    for index, address in enumerate(addresses, 1):
        config += "IP." + str(index) + " = " + address + "\n"
    return config
