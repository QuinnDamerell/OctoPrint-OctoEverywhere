import errno
import socket
import time
import unittest
from http.client import IncompleteRead
from unittest.mock import patch

import requests
import urllib3

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

# pylint: disable=wrong-import-position,wrong-import-order,protected-access
import octowebsocket  # noqa: E402
from octoeverywhere.sentry import Sentry  # noqa: E402


class TestSentryConnectionErrors(unittest.TestCase):
    def test_common_connection_errors_do_not_depend_on_english_messages(self) -> None:
        errors = [
            TimeoutError("Anslutningsförsöket misslyckades"),
            OSError(errno.EHOSTUNREACH, "untranslated error"),
            OSError(errno.ENETUNREACH, "untranslated error"),
            BrokenPipeError(errno.EPIPE, "untranslated error"),
            ConnectionAbortedError(errno.ECONNABORTED, "untranslated error"),
            socket.gaierror(socket.EAI_AGAIN, "untranslated error"),
            requests.exceptions.ConnectionError(ConnectionRefusedError(errno.ECONNREFUSED, "printer is offline")),
            requests.exceptions.ConnectTimeout("printer is offline"),
            requests.exceptions.ReadTimeout("printer stopped responding"),
            octowebsocket.WebSocketAddressException("Temporary failure in name resolution"),
        ]
        windowsError = OSError("untranslated error")
        windowsError.winerror = 10065
        errors.append(windowsError)
        for error in errors:
            with self.subTest(error=error):
                self.assertTrue(Sentry.IsCommonConnectionException(error))


    def test_unexpected_errors_are_still_reportable(self) -> None:
        for error in [OSError(errno.EIO, "read failure"), PermissionError(errno.EACCES, "denied"),
                      RuntimeError("Connection timed out"), requests.exceptions.SSLError("invalid TLS record")]:
            with self.subTest(error=error):
                self.assertFalse(Sentry.IsCommonConnectionException(error))
                self.assertFalse(Sentry.IsCommonHttpError(error))


    def test_websocket_dns_wrapper_preserves_errno_even_with_short_messages(self) -> None:
        dnsError = socket.gaierror(socket.EAI_AGAIN, "Try again")
        self.assertTrue(Sentry.IsCommonConnectionException(octowebsocket.WebSocketAddressException(dnsError)))
        error = octowebsocket.WebSocketAddressException(str(dnsError))
        error.__context__ = dnsError
        self.assertTrue(Sentry.IsCommonConnectionException(error))
        self.assertTrue(Sentry.IsCommonConnectionException(
            octowebsocket.WebSocketAddressException("nodename nor servname provided, or not known")))
        error.__context__ = ValueError("unexpected resolver bug")
        self.assertFalse(Sentry.IsCommonConnectionException(error))


    def test_request_connection_wrappers_preserve_resource_and_protocol_errors(self) -> None:
        for cause in (OSError(errno.EMFILE, "too many open files"), OSError(errno.ENOMEM, "out of memory"),
                      urllib3.exceptions.ProtocolError("malformed response"), requests.exceptions.SSLError("bad TLS record")):
            with self.subTest(cause=cause):
                error = requests.exceptions.ConnectionError(cause)
                self.assertFalse(Sentry.IsCommonConnectionException(error))
                self.assertFalse(Sentry.IsCommonHttpError(error))


    def test_request_retry_wrapper_checks_original_socket_failure(self) -> None:
        for socketError, expected in ((ConnectionRefusedError(errno.ECONNREFUSED, "offline"), True),
                                      (OSError(errno.EMFILE, "too many open files"), False),
                                      (OSError(errno.ENOMEM, "out of memory"), False)):
            with self.subTest(socketError=socketError):
                connectionError = urllib3.exceptions.NewConnectionError(None, "connection failed")
                connectionError.__cause__ = socketError
                retryError = urllib3.exceptions.MaxRetryError(None, "/api/version", connectionError)
                error = requests.exceptions.ConnectionError(retryError)
                self.assertEqual(Sentry.IsCommonConnectionException(error), expected)
                self.assertEqual(Sentry.IsCommonHttpError(error), expected)


    def test_http_interrupted_reads_require_a_known_typed_cause(self) -> None:
        for cause in [IncompleteRead(b"", 123), ConnectionResetError(errno.ECONNRESET, "reset"),
                      ConnectionAbortedError(errno.ECONNABORTED, "aborted"), BrokenPipeError(errno.EPIPE, "closed")]:
            protocolError = urllib3.exceptions.ProtocolError("Connection broken", cause)
            with self.subTest(cause=cause):
                self.assertTrue(Sentry.IsCommonHttpError(protocolError))
                self.assertTrue(Sentry.IsCommonHttpError(requests.exceptions.ChunkedEncodingError(protocolError)))
        for error in [urllib3.exceptions.ProtocolError("bad framing"),
                      urllib3.exceptions.ProtocolError("unexpected", ValueError("bad header")),
                      requests.exceptions.InvalidURL("missing URL"), requests.exceptions.RequestException("unexpected")]:
            with self.subTest(error=error):
                self.assertFalse(Sentry.IsCommonHttpError(error))


    def test_report_budget_allows_five_events_per_window(self) -> None:
        with patch.multiple(Sentry, FilterExceptionsByPackage=False, LastErrorReport=time.time(), LastErrorCount=0):
            for _ in range(5):
                self.assertEqual(Sentry._beforeSendFilter({"event": True}, {}), {"event": True})
            self.assertIsNone(Sentry._beforeSendFilter({"event": True}, {}))


if __name__ == "__main__":
    unittest.main()
