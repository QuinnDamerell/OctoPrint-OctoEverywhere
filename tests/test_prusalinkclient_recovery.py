import logging
import unittest
from unittest.mock import MagicMock, patch

import requests

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

# pylint: disable=wrong-import-position,protected-access
from prusalink_octoeverywhere.prusalinkclient import PrusaLinkClient  # noqa: E402
from octoeverywhere.exceptions import NoSentryReportException  # noqa: E402


class TestPrusaLinkClientRecovery(unittest.TestCase):
    def test_only_bad_gateway_poll_response_is_expected_retry(self) -> None:
        client = PrusaLinkClient.__new__(PrusaLinkClient)
        response = MagicMock()
        response.status_code = 502
        response.text = "shim upstream error: No route to host"
        client._Request = MagicMock(return_value=response)
        with self.assertRaisesRegex(NoSentryReportException, "No route to host"):
            client._GetJson("/api/version")
        response.status_code = 500
        with self.assertRaises(Exception) as caught:
            client._GetJson("/api/version")
        self.assertNotIsInstance(caught.exception, NoSentryReportException)


    def test_offline_job_command_returns_failure_without_reporting(self) -> None:
        client = PrusaLinkClient.__new__(PrusaLinkClient)
        client.Logger = logging.getLogger("TestPrusaLinkClientRecovery")
        client.GetState = MagicMock()
        client.GetState.return_value.JobId = 1
        for error in (requests.exceptions.ConnectionError(ConnectionRefusedError("offline")), requests.exceptions.ReadTimeout("offline")):
            client._Request = MagicMock(side_effect=error)
            with self.subTest(error=error), patch("prusalink_octoeverywhere.prusalinkclient.Sentry.OnException") as report:
                self.assertFalse(client._SendJobAction("pause"))
                report.assert_not_called()


    def test_offline_polling_keeps_normal_backoff(self) -> None:
        self._AssertRetry(requests.exceptions.ConnectTimeout("offline"))
        self._AssertRetry(NoSentryReportException("local gateway returned 502"))


    def _AssertRetry(self, error:Exception) -> None:
        class StopWorker(BaseException):
            pass

        client = PrusaLinkClient.__new__(PrusaLinkClient)
        client.Logger = logging.getLogger("TestPrusaLinkClientRecovery")
        client._CleanupStateOnDisconnect = MagicMock()
        client._GetConnectionContextToTry = MagicMock(side_effect=error)
        client.StateTranslator = MagicMock()
        client.ConnectionFinalized = False
        client.ConsecutivelyFailedConnectionAttempts = 0
        client.SleepEvent = MagicMock()
        client.SleepEvent.wait.side_effect = StopWorker()
        with patch("prusalink_octoeverywhere.prusalinkclient.LocalWebApi.Get"), \
             patch("prusalink_octoeverywhere.prusalinkclient.Sentry.OnException") as report:
            with self.assertRaises(StopWorker):
                client._ClientWorker()
            client.SleepEvent.wait.assert_called_once_with(5.0)
            report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
