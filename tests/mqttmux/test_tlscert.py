import logging
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from octoeverywhere.mqttmux import tlscert


def _handshake(context, public_certificate, hostname="localhost"):
    """Perform an actual TLS handshake and data exchange with a verified client."""
    client_context = ssl.create_default_context(cafile=str(public_certificate))
    client_context.maximum_version = ssl.TLSVersion.TLSv1_2
    client_input, client_output = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_input, server_output = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(client_input, client_output, server_hostname=hostname)
    server = context.wrap_bio(server_input, server_output, server_side=True)
    client_done = server_done = False
    for _ in range(10):
        try:
            client.do_handshake()
            client_done = True
        except ssl.SSLWantReadError:
            pass
        server_input.write(client_output.read())
        try:
            server.do_handshake()
            server_done = True
        except ssl.SSLWantReadError:
            pass
        client_input.write(server_output.read())
        if client_done and server_done:
            client.write(b"mqtt tls")
            server_input.write(client_output.read())
            return server.version(), server.read()
    raise AssertionError("TLS handshake did not finish")


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL is required for TLS certificate integration tests")
class TestTlsCertificate(unittest.TestCase):

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.storage = self.temporary.name
        self.pem = Path(self.storage) / "mqtt-tls" / "server.pem"
        self.public_certificate = self.pem.with_suffix(".crt")
        self.logger = logging.getLogger("mqttmux.tlscert.test")

    def _create(self, **kwargs):
        return tlscert.CreateServerTlsContext(self.logger, self.storage, **kwargs)

    def test_generated_certificate_performs_verified_tls12_handshake(self):
        context = self._create(bind="192.0.2.7")
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(_handshake(context, self.public_certificate), ("TLSv1.2", b"mqtt tls"))
        self.assertEqual(_handshake(context, self.public_certificate, "192.0.2.7"), ("TLSv1.2", b"mqtt tls"))
        self.assertNotIn(b"PRIVATE KEY", self.public_certificate.read_bytes())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.pem.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.pem.parent.stat().st_mode), 0o700)

    def test_restart_reuses_certificate_and_restores_public_export(self):
        self._create()
        original = self.pem.read_bytes()
        self.public_certificate.unlink()
        context = self._create()
        self.assertEqual(self.pem.read_bytes(), original)
        self.assertEqual(_handshake(context, self.public_certificate), ("TLSv1.2", b"mqtt tls"))

    def test_separate_installations_get_unique_private_keys(self):
        self._create()
        with tempfile.TemporaryDirectory() as other_storage:
            tlscert.CreateServerTlsContext(self.logger, other_storage)
            other_pem = (Path(other_storage) / "mqtt-tls" / "server.pem").read_bytes()
        self.assertNotEqual(self._key(self.pem.read_bytes()), self._key(other_pem))

    def test_multiple_relays_can_share_one_client_trust_bundle(self):
        first_context = self._create()
        with tempfile.TemporaryDirectory() as other_storage:
            second_context = tlscert.CreateServerTlsContext(self.logger, other_storage)
            second_certificate = (Path(other_storage) / "mqtt-tls" / "server.crt").read_bytes()
        trust_bundle = Path(self.storage) / "trusted-relays.crt"
        trust_bundle.write_bytes(self.public_certificate.read_bytes() + second_certificate)
        for context in (first_context, second_context):
            self.assertEqual(_handshake(context, trust_bundle), ("TLSv1.2", b"mqtt tls"))

    def test_renewal_replaces_certificate_but_preserves_private_key(self):
        self._create()
        original = self.pem.read_bytes()
        # Trigger renewal without depending on the test machine's wall clock.
        with patch.object(tlscert, "_RenewBeforeSeconds", 400 * 24 * 60 * 60):
            context = self._create()
        renewed = self.pem.read_bytes()
        self.assertNotEqual(renewed, original)
        self.assertEqual(self._key(renewed), self._key(original))
        self.assertEqual(_handshake(context, self.public_certificate), ("TLSv1.2", b"mqtt tls"))
        self.assertEqual(renewed.count(b"BEGIN CERTIFICATE"), 1)

    def test_failed_renewal_keeps_existing_credentials(self):
        self._create()
        original = self.pem.read_bytes()
        with patch.object(tlscert, "_CertificateIsCurrent", return_value=False), \
                patch.object(tlscert, "_RunOpenSsl", side_effect=RuntimeError("generation failed")):
            with self.assertRaisesRegex(RuntimeError, "generation failed"):
                self._create()
        self.assertEqual(self.pem.read_bytes(), original)
        self.assertFalse(list(self.pem.parent.glob(".generate-*")))

    def test_custom_combined_certificate_needs_no_openssl_and_is_not_modified(self):
        self._create()
        original = self.pem.read_bytes()
        with patch.object(tlscert.shutil, "which", return_value=None):
            context = self._create(cert_path=str(self.pem))
        self.assertEqual(self.pem.read_bytes(), original)
        self.assertEqual(_handshake(context, self.public_certificate), ("TLSv1.2", b"mqtt tls"))

    def test_separate_custom_certificate_and_key(self):
        self._create()
        key_path = Path(self.storage) / "custom.key"
        key_path.write_bytes(self._key(self.pem.read_bytes()))
        context = self._create(cert_path=str(self.public_certificate), key_path=str(key_path))
        self.assertEqual(_handshake(context, self.public_certificate), ("TLSv1.2", b"mqtt tls"))

    def test_invalid_custom_credentials_are_rejected_without_generation(self):
        with patch.object(tlscert, "_RunOpenSsl") as run:
            with self.assertRaises(ValueError):
                self._create(key_path="unused.key")
            with self.assertRaises(OSError):
                self._create(cert_path="missing.pem")
        run.assert_not_called()

    def test_missing_openssl_has_actionable_error(self):
        with patch.object(tlscert.shutil, "which", return_value=None), \
                patch.object(tlscert.os.path, "isfile", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "install openssl"):
                self._create()

    def test_openssl_timeout_is_bounded_and_no_shell_is_used(self):
        with patch.object(tlscert.subprocess, "run", side_effect=subprocess.TimeoutExpired("openssl", 60)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                self._create()
        self.assertEqual(run.call_args[1]["timeout"], 60)
        self.assertNotIn("shell", run.call_args[1])
        self.assertIsInstance(run.call_args[0][0], list)
        self.assertFalse(list(self.pem.parent.glob(".generate-*")))

    def test_untrusted_host_strings_cannot_inject_openssl_config(self):
        with patch.object(tlscert.socket, "gethostname", return_value="printer-host"):
            config = tlscert._CertificateConfig("evil\nkeyUsage = keyCertSign")
            self.assertNotIn("keyCertSign", config)
            self.assertIn("printer-host", config)
            self.assertNotIn("0.0.0.0", tlscert._CertificateConfig("0.0.0.0"))

    def test_corrupt_generated_credentials_are_replaced(self):
        self.pem.parent.mkdir()
        self.pem.write_text("corrupt", encoding="ascii")
        context = self._create()
        self.assertEqual(_handshake(context, self.public_certificate), ("TLSv1.2", b"mqtt tls"))

    def test_changed_bind_address_reuses_valid_certificate(self):
        self._create(bind="192.0.2.7")
        original = self.pem.read_bytes()
        original_certificate = self.public_certificate.read_bytes()
        trusted_certificate = Path(self.storage) / "client-trust.crt"
        trusted_certificate.write_bytes(original_certificate)

        context = self._create(bind="192.0.2.8")
        self.assertEqual(self.pem.read_bytes(), original)
        self.assertEqual(self.public_certificate.read_bytes(), original_certificate)
        self.assertEqual(_handshake(context, trusted_certificate), ("TLSv1.2", b"mqtt tls"))

    def test_docker_hostname_change_preserves_certificate_and_existing_client_trust(self):
        with patch.object(tlscert.socket, "gethostname", return_value="98c2e2f4184a"):
            self._create(bind="192.0.2.7")
        original = self.pem.read_bytes()
        original_certificate = self.public_certificate.read_bytes()
        # A client keeps the certificate it previously imported, independently
        # of any certificate the recreated container exports on startup.
        trusted_certificate = Path(self.storage) / "client-trust.crt"
        trusted_certificate.write_bytes(original_certificate)
        legacy_san = self.pem.with_suffix(".san")

        for metadata in (None, "old-container-hostname\n127.0.0.1\n"):
            with self.subTest(legacy_metadata=metadata):
                if metadata is None:
                    if legacy_san.exists():
                        legacy_san.unlink()
                else:
                    legacy_san.write_text(metadata, encoding="ascii")
                with patch.object(tlscert.socket, "gethostname", return_value="4c461df3aa95"):
                    context = self._create(bind="192.0.2.7")
                self.assertEqual(self.pem.read_bytes(), original)
                self.assertEqual(self._key(self.pem.read_bytes()), self._key(original))
                self.assertEqual(self.public_certificate.read_bytes(), original_certificate)
                self.assertEqual(_handshake(context, trusted_certificate, "192.0.2.7"),
                                 ("TLSv1.2", b"mqtt tls"))

    def test_chmod_failure_does_not_disable_tls(self):
        self._create()
        with patch.object(tlscert.os, "chmod", side_effect=PermissionError("not owner")):
            context = self._create()
        self.assertEqual(_handshake(context, self.public_certificate), ("TLSv1.2", b"mqtt tls"))

    @staticmethod
    def _key(pem):
        return re.search(br"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----.*?"
                         br"-----END (?:RSA |EC )?PRIVATE KEY-----", pem, re.DOTALL).group(0)
